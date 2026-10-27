# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""Place FMC firewalls: one area each, HA side by side, interfaces on dummies."""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from .fmc_reader import FmcData, FmcDevice, FmcInterface
from .fmc_stencil_mapper import map_firewall, map_fmc, map_switch, map_waypoint
from .ns_model import (
    NSDevice, NSIPAssignment, NSL1Link, NSModel, NSPortChannel, NSSubInterface,
    NSVirtualPort, normalise_port_name,
)

FMC_AREA = "FMC"
WAN_AREA = "wan_wp_"
DUMMY_FMC = "Dummy_fmc"
DUMMY_WAN = "Dummy_wan"
GRAY = (200, 200, 200)
_AIR = "_AIR_"

_DEFAULT_KEYWORDS = {
    "management": ["management", "mgmt", "diagnostic"],
    "top": ["outside", "internet", "wan", "untrust", "external", "public"],
    "right": ["dmz"],
    "bottom": ["inside", "internal", "lan", "trust", "users"],
}
_FAILOVER = ("failover", "fover", "state-link")


@dataclass
class MapperReport:
    devices: int
    l1_links: int
    notes: List[str]
    placements: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)


@dataclass
class _Drawn:
    role: str
    device: str
    title: str
    ports: List[str]
    ip_port: str
    cidrs: List[str]
    subifs: List[Tuple[str, str, Optional[int], List[str]]] = field(default_factory=list)
    portchannel: str = ""


@dataclass
class _Group:
    area: str
    members: List[Tuple[FmcDevice, str]]  # device, ns name


def _keywords(cfg: Dict[str, Any]) -> Dict[str, List[str]]:
    found: Dict[str, List[str]] = {}
    for role, default in _DEFAULT_KEYWORDS.items():
        raw = cfg.get(f"{role}_keywords", default)
        if isinstance(raw, str):
            raw = [part.strip() for part in raw.split(",") if part.strip()]
        if not isinstance(raw, list) or not raw:
            raw = default
        found[role] = [str(item).strip().lower() for item in raw if str(item).strip()]
    return found


def _hits(text: str, words: Sequence[str]) -> bool:
    return any(word and word in text for word in words)


def classify_interface(iface: FmcInterface, keywords: Dict[str, List[str]]) -> str:
    """Return management, failover, top, right, or bottom. First match wins."""
    text = " ".join((iface.ifname, iface.zone, iface.name)).lower()
    if iface.management_only or _hits(text, keywords["management"]):
        return "management"
    if _hits(text, _FAILOVER):
        return "failover"
    if _hits(text, keywords["top"]):
        return "top"
    if _hits(text, keywords["right"]):
        return "right"
    if _hits(text, keywords["bottom"]):
        return "bottom"
    return "bottom"


def gateway_cidr(gateway: str, interface_cidrs: Union[str, Sequence[str]]) -> str:
    """Put the next hop in the interface subnet of the same address family.

    Without a matching subnet the next hop is a host route (/32 or /128).
    """
    text = (gateway or "").strip()
    if not text:
        return ""
    if "/" in text:
        return text
    try:
        host = ipaddress.ip_address(text)
    except ValueError:
        return ""
    if isinstance(interface_cidrs, str):
        interface_cidrs = [interface_cidrs] if interface_cidrs else []
    for cidr in interface_cidrs:
        try:
            iface = ipaddress.ip_interface(cidr)
        except ValueError:
            continue
        if iface.version == host.version and host in iface.network:
            return f"{host.compressed}/{iface.network.prefixlen}"
    return f"{host.compressed}/{host.max_prefixlen}"


def facing_port(firewall_port: str) -> str:
    """Dummy port facing a firewall. Management is always GigabitEthernet."""
    port = normalise_port_name(firewall_port)
    type_token, _, number = port.partition(" ")
    if type_token.casefold() in {"management", "mgmt"}:
        type_token = "GigabitEthernet"
        number = number or "0"
    if not type_token:
        return "GigabitEthernet 0"
    return f"{type_token} {number}" if number else type_token


def sanitize_name(raw: str) -> str:
    text = (raw or "").strip()
    for char in "/\\'":
        text = text.replace(char, "_")
    return text.strip()


def _drawable(iface: FmcInterface) -> bool:
    if iface.enabled:
        return True
    return bool(iface.ifname or iface.zone)


def _parent_port(iface: FmcInterface) -> str:
    if "." not in iface.name:
        return ""
    head, tail = iface.name.rsplit(".", 1)
    if not tail.isdigit():
        return ""
    return normalise_port_name(head)


def _logical_port(raw: str) -> str:
    port = normalise_port_name(raw)
    if port and " " not in port:
        for index, char in enumerate(port):
            if char.isdigit():
                return f"{port[:index].rstrip()} {port[index:]}"
    return port


def _vlan(iface: FmcInterface) -> Optional[int]:
    if iface.vlan is not None:
        return iface.vlan
    if "." in iface.name:
        tail = iface.name.rsplit(".", 1)[1]
        if tail.isdigit():
            return int(tail)
    return None


class _Canvas:
    def __init__(self, keywords: Dict[str, List[str]]) -> None:
        self.keywords = keywords
        self.model = NSModel()
        self.used: set = set()
        self.notes: List[str] = []
        self.placements: List[str] = []
        self.skipped: List[str] = []
        self._ports: Dict[str, int] = {}
        self._mgmt: List[str] = []
        self.dummy_fmc = DUMMY_FMC
        self.dummy_wan = DUMMY_WAN
        self.fmc_name = FMC_AREA
        self.fmc_ipv4 = ""
        self.fmc_gateway = ""
        self.facing: Dict[Tuple[str, str], Tuple[str, str, str]] = {}

    def claim(self, raw: str) -> str:
        base = sanitize_name(raw) or "device"
        candidate = base
        number = 2
        while candidate.casefold() in self.used:
            candidate = f"{base}_{number}"
            number += 1
        self.used.add(candidate.casefold())
        return candidate

    def add_device(self, name: str, area: str, row: int, stencil, color=None) -> None:
        self.model.devices[name] = NSDevice(
            name=name, area=area, row=row, stencil=stencil,
            is_endpoint=False, default_color=color,
        )

    def ethernet(self, device: str) -> str:
        number = self._ports.get(device, 0)
        self._ports[device] = number + 1
        return f"Ethernet {number}"

    def link(self, a: str, a_port: str, b: str, b_port: str) -> None:
        self.model.l1_links.append(NSL1Link(a, a_port, b, b_port))

    def add_ip(self, device: str, port: str, cidr: str) -> None:
        if not cidr:
            return
        for item in self.model.ip_assignments:
            if item.device == device and item.port == port:
                if cidr not in item.cidrs:
                    item.cidrs.append(cidr)
                return
        self.model.ip_assignments.append(NSIPAssignment(device, port, [cidr]))

    def add_ips(self, device: str, port: str, cidrs: Sequence[str]) -> None:
        for cidr in cidrs:
            self.add_ip(device, port, cidr)

    def _dummy(self, area: str, role: str, counters: Dict[str, int]) -> str:
        counters[role] = counters.get(role, 0) + 1
        number = counters[role]
        if role == "management":
            raw = f"{area}_Dummy_mgmt{number}"
        else:
            label = {"top": "Outside", "right": "DMZ", "bottom": "Inside"}[role]
            raw = f"{area}_Dummy_{label}" if number == 1 else f"{area}_Dummy_{label}{number}"
        name = self.claim(raw)
        self.add_device(name, area, 0, map_switch(name, "synthesised interface direction"), GRAY)
        if role == "management":
            self._mgmt.append(name)
        return name

    def _finish(self, drawn: _Drawn, dummy: str) -> None:
        first_dummy_port = ""
        for port in drawn.ports:
            dummy_port = facing_port(port)
            if not first_dummy_port:
                first_dummy_port = dummy_port
            self.link(drawn.device, port, dummy, dummy_port)
        if drawn.portchannel:
            self.model.port_channels.append(
                NSPortChannel(drawn.device, list(drawn.ports), drawn.portchannel)
            )
            self.add_ips(drawn.device, drawn.portchannel, drawn.cidrs)
        else:
            self.add_ips(drawn.device, drawn.ip_port, drawn.cidrs)
        for parent, subif, vlan, cidrs in drawn.subifs:
            self.model.subinterfaces.append(
                NSSubInterface(drawn.device, parent, subif, vlan)
            )
            self.add_ips(drawn.device, subif, cidrs)
        self.placements.append(
            f"{drawn.device} {drawn.title} -> {drawn.role} {dummy}"
        )
        if drawn.title and first_dummy_port:
            self.facing[(drawn.device, drawn.title.casefold())] = (
                dummy, first_dummy_port, list(drawn.cidrs),
            )

    def _apply_gateways(self, device: FmcDevice, ns_name: str) -> None:
        routes = [
            route for route in device.static_routes
            if route.is_default and not route.tunneled and route.gateway
        ]
        routes.sort(key=lambda route: route.metric)
        seen = set()
        for route in routes:
            try:
                hop = ipaddress.ip_address(route.gateway.split("/", 1)[0].strip())
            except ValueError:
                self.notes.append(
                    f"{ns_name}: default gateway {route.gateway} is not an IP address"
                )
                continue
            key = (ns_name, route.interface_name.casefold(), hop.version)
            if key in seen:
                continue
            seen.add(key)
            if hop.is_link_local:
                self.notes.append(
                    f"{ns_name}: IPv6 default gateway {route.gateway} on "
                    f"{route.interface_name} is link-local, which Network Sketcher "
                    "does not accept, so it is not drawn"
                )
                continue
            facing = self.facing.get(key[:2])
            if facing is None:
                self.notes.append(
                    f"{ns_name}: default gateway {route.gateway} on {route.interface_name} "
                    "has no matching interface"
                )
                continue
            dummy, port, cidrs = facing
            assigned = gateway_cidr(route.gateway, cidrs)
            self.add_ip(dummy, port, assigned)
            self.placements.append(
                f"{ns_name} default gateway {assigned} -> {dummy} {port}"
            )

    def _peer(self, left: _Drawn, right: _Drawn) -> None:
        if len(left.ports) == len(right.ports):
            pairs = list(zip(left.ports, right.ports))
        else:
            pairs = [(left.ports[0], right.ports[0])]
        for left_port, right_port in pairs:
            self.link(left.device, left_port, right.device, right_port)
        for drawn in (left, right):
            if drawn.portchannel:
                self.model.port_channels.append(
                    NSPortChannel(drawn.device, list(drawn.ports), drawn.portchannel)
                )
                self.add_ips(drawn.device, drawn.portchannel, drawn.cidrs)
            else:
                self.add_ips(drawn.device, drawn.ip_port, drawn.cidrs)
            for parent, subif, vlan, cidrs in drawn.subifs:
                self.model.subinterfaces.append(
                    NSSubInterface(drawn.device, parent, subif, vlan)
                )
                self.add_ips(drawn.device, subif, cidrs)
            self.placements.append(
                f"{drawn.device} {drawn.title} -> failover peer"
            )

    def _collect(self, device: FmcDevice, ns_name: str) -> Tuple[List[_Drawn], List[_Drawn]]:
        regular: List[_Drawn] = []
        failover: List[_Drawn] = []
        members: set = set()
        for iface in device.interfaces:
            if iface.kind == "etherchannel" and _drawable(iface):
                for member in iface.members:
                    members.add(normalise_port_name(member).casefold())

        by_port: Dict[str, _Drawn] = {}

        def take(role: str) -> List[_Drawn]:
            return failover if role == "failover" else regular

        for iface in device.interfaces:
            if iface.kind in {"subinterface", "loopback"}:
                continue
            if not _drawable(iface):
                self.skipped.append(f"{ns_name} {iface.name or '(unnamed)'}")
                continue
            port = _logical_port(iface.name)
            if iface.kind != "etherchannel" and port.casefold() in members:
                continue
            role = classify_interface(iface, self.keywords)
            title = iface.ifname or iface.name or port
            if iface.kind == "etherchannel":
                if not iface.members:
                    self.notes.append(f"{ns_name} {iface.name}: EtherChannel has no members")
                    continue
                ports = [normalise_port_name(member) for member in iface.members]
                drawn = _Drawn(
                    role, ns_name, title, ports, port, iface.cidrs,
                    portchannel=port or "Port-channel 1",
                )
            else:
                drawn = _Drawn(role, ns_name, title, [port], port, iface.cidrs)
            take(role).append(drawn)
            for owned in drawn.ports:
                by_port[owned.casefold()] = drawn
            by_port[port.casefold()] = drawn

        for iface in device.interfaces:
            if iface.kind != "subinterface":
                continue
            if not _drawable(iface):
                self.skipped.append(f"{ns_name} {iface.name or '(unnamed)'}")
                continue
            parent = _parent_port(iface)
            subif = normalise_port_name(iface.name)
            if not parent or not subif:
                self.notes.append(f"{ns_name} {iface.name}: sub-interface has no parent")
                continue
            existing = by_port.get(parent.casefold())
            role = classify_interface(iface, self.keywords)
            if existing is None:
                drawn = _Drawn(role, ns_name, iface.ifname or iface.name, [parent], parent, [])
                take(role).append(drawn)
                by_port[parent.casefold()] = drawn
                existing = drawn
            bind = parent
            if existing.portchannel and parent.casefold() == existing.ip_port.casefold():
                bind = existing.ports[0]
                self.notes.append(
                    f"{ns_name} {subif}: bound to {bind} because the port-channel "
                    "is created after physical links"
                )
            existing.subifs.append((bind, subif, _vlan(iface), iface.cidrs))

        for iface in device.interfaces:
            if iface.kind != "loopback" or not _drawable(iface):
                continue
            port = normalise_port_name(iface.name) or "Loopback 0"
            self.model.virtual_ports.append(
                NSVirtualPort(ns_name, port, is_loopback=True)
            )
            self.add_ips(ns_name, port, iface.cidrs)
        return regular, failover

    def place_group(self, group: _Group) -> None:
        drawn_by_device: Dict[str, Tuple[List[_Drawn], List[_Drawn]]] = {}
        for device, ns_name in group.members:
            drawn_by_device[ns_name] = self._collect(device, ns_name)

        ordered = [ns_name for _device, ns_name in group.members]
        if len(ordered) == 2:
            left_fail = drawn_by_device[ordered[0]][1]
            right_fail = drawn_by_device[ordered[1]][1]
            if len(left_fail) == 1 and len(right_fail) == 1 and left_fail[0].ports and right_fail[0].ports:
                self._peer(left_fail[0], right_fail[0])
                left_fail.clear()
                right_fail.clear()
            else:
                if left_fail or right_fail:
                    self.notes.append(
                        f"{group.area}: failover interfaces were not paired, so they sit on the bottom row"
                    )

        counters: Dict[str, int] = {}
        columns: Dict[str, List[str]] = {"management": [], "top": [], "right": [], "bottom": []}
        for ns_name in ordered:
            regular, failover = drawn_by_device[ns_name]
            for item in failover:
                item.role = "bottom"
                regular.append(item)
            for role in ("management", "top", "right", "bottom"):
                for item in regular:
                    if item.role != role:
                        continue
                    dummy = self._dummy(group.area, role, counters)
                    columns[role].append(dummy)
                    self._finish(item, dummy)

        top = columns["management"] + columns["top"]
        mid = ordered + columns["right"]
        bottom = columns["bottom"]
        rows: List[List[str]] = []
        if top:
            rows.append(top)
        rows.append(mid)
        if bottom:
            rows.append(bottom)
        width = max(len(row) for row in rows)
        grid = [row + [_AIR] * (width - len(row)) for row in rows]
        self.model.area_to_devices[group.area] = grid
        for index, row in enumerate(grid):
            for name in row:
                if name != _AIR and name in self.model.devices:
                    self.model.devices[name].row = index
        for device, ns_name in group.members:
            self._apply_gateways(device, ns_name)

    def connect_management(self) -> None:
        fmc_port = self.ethernet(self.fmc_name)
        dummy_port = self.ethernet(self.dummy_fmc)
        self.link(self.fmc_name, fmc_port, self.dummy_fmc, dummy_port)
        if self.fmc_ipv4:
            own = self.fmc_ipv4 if "/" in self.fmc_ipv4 else gateway_cidr(self.fmc_ipv4, [])
            self.add_ip(self.fmc_name, fmc_port, own)
        if self.fmc_gateway:
            assigned = gateway_cidr(self.fmc_gateway, self.fmc_ipv4)
            self.add_ip(self.dummy_fmc, dummy_port, assigned)
            self.placements.append(
                f"{self.fmc_name} default gateway {assigned} -> {self.dummy_fmc} {dummy_port}"
            )
        self.link(
            self.dummy_wan, self.ethernet(self.dummy_wan),
            self.dummy_fmc, self.ethernet(self.dummy_fmc),
        )
        for dummy in self._mgmt:
            self.link(dummy, self.ethernet(dummy), self.dummy_wan, self.ethernet(self.dummy_wan))


def _groups(data: FmcData, canvas: _Canvas) -> List[_Group]:
    by_id = {device.id: device for device in data.devices if device.id}
    paired: set = set()
    pending: List[Tuple[str, List[FmcDevice]]] = []
    for pair in data.ha_pairs:
        primary = by_id.get(pair.primary_id)
        secondary = by_id.get(pair.secondary_id)
        if primary is None or secondary is None:
            canvas.notes.append(f"HA pair {pair.name or pair.primary_id}: a member device is missing")
            continue
        if primary.id in paired or secondary.id in paired:
            continue
        paired.update({primary.id, secondary.id})
        pending.append((primary.hostname or primary.name or pair.name, [primary, secondary]))
    for device in data.devices:
        if device.id and device.id in paired:
            continue
        pending.append((device.hostname or device.name, [device]))

    named: List[_Group] = []
    for raw_area, devices in pending:
        sort_key = sanitize_name(raw_area) or "firewall"
        members = []
        for device in devices:
            label = device.hostname or device.name or sort_key
            members.append((device, canvas.claim(label)))
        named.append(_Group(sort_key, members))
    named.sort(key=lambda group: group.area.casefold())
    for index, group in enumerate(named, start=1):
        group.area = f"Dummy_Area_{index}"
    return named


def build_model(data: FmcData, cfg: Optional[Dict[str, Any]] = None):
    """Return ``(model, devices, report)``."""
    canvas = _Canvas(_keywords(cfg or {}))
    fmc_name = canvas.claim(data.fmc_hostname or data.fmc_name or FMC_AREA)
    canvas.fmc_name = fmc_name
    canvas.fmc_ipv4 = data.fmc_ipv4
    canvas.fmc_gateway = data.fmc_gateway
    canvas.dummy_fmc = canvas.claim(DUMMY_FMC)
    canvas.dummy_wan = canvas.claim(DUMMY_WAN)
    canvas.add_device(fmc_name, FMC_AREA, 0, map_fmc(fmc_name, data.fmc_version))
    canvas.add_device(
        canvas.dummy_fmc, FMC_AREA, 1,
        map_switch(canvas.dummy_fmc, "synthesised management switch"), GRAY,
    )
    canvas.add_device(
        canvas.dummy_wan, WAN_AREA, 0, map_waypoint(canvas.dummy_wan), GRAY,
    )
    canvas.model.area_to_devices[FMC_AREA] = [[fmc_name], [canvas.dummy_fmc]]
    canvas.model.area_to_devices[WAN_AREA] = [[canvas.dummy_wan]]

    groups = _groups(data, canvas)
    for group in groups:
        for device, ns_name in group.members:
            canvas.add_device(
                ns_name, group.area, 0,
                map_firewall(ns_name, device.model, device.version),
            )
        canvas.place_group(group)
    canvas.connect_management()

    areas = [group.area for group in groups]
    canvas.model.areas = [[FMC_AREA], [WAN_AREA]] + ([areas] if areas else [])
    report = MapperReport(
        devices=len(canvas.model.devices),
        l1_links=len(canvas.model.l1_links),
        notes=canvas.notes,
        placements=canvas.placements,
        skipped=canvas.skipped,
    )
    return canvas.model, canvas.model.devices, report
