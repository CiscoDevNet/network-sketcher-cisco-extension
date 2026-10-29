# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0
"""Shared drawing state for the integrated CML overlay.

Every overlay builder (EVPN, SD-WAN, L3VPN) and the stitch step write into
one :class:`OverlayCanvas`. It owns the ``NSModel`` plus what the builders
need to agree on: which area and row each device sits in, the next free
``Dummy N`` port of a logical device, the ports that already carry a link,
and the notes that end up in ``overlay_report.md``.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional, Set, Tuple

from .stencil_mapper import StencilMapping
from .topology_mapper import (
    NSDevice, NSIPAssignment, NSL1Link, NSL2Segment, NSModel, NSPortChannel, NSVirtualPort,
)

# Default-cell colours (RULE 16 palette shared with nd / sdwan converters).
COLOR_OVERLAY = (221, 204, 255)    # light purple -- overlay / logical constructs
COLOR_SYNTHETIC = (200, 200, 200)  # gray -- drawing devices with no CML node

# Rows inside a site area, top to bottom.
ROW_EDGE = 0      # SD-WAN Edge / L3VPN PE
ROW_GATEWAY = 1   # VRF-GW (EVPN distributed anycast gateway)
ROW_NETWORK = 2   # NET (L2VNI) / LAN switch / LAN dummy
ROW_HOST = 3      # hosts / CE routers

CLOUD_SVI_PORT = "Dummy 0"


def logical_stencil(name: str, kind: str, stencil_type: str, model: str, os_name: str = "") -> StencilMapping:
    """Stencil for a device the converter draws without a CML node."""
    return StencilMapping(
        label=name, node_definition=kind, image_definition="",
        stencil_type=stencil_type, model=model, os=os_name,
        confidence=0.60, reason=f"overlay construct '{kind}'", tags=[kind],
    )


@dataclass
class Handoff:
    """A CML L1 link whose two ends belong to (different) overlay contexts."""
    a_device: str
    a_port: str
    a_context: str          # e.g. 'VPN 10' / 'VRF BLUE'
    b_device: str
    b_port: str
    b_context: str
    status: str             # 'confirmed' (same subnet) / 'inferred'
    drawn: bool = True
    note: str = ""


@dataclass
class Attachment:
    """A LAN-facing VRF / VPN port that the stitch step connects.

    ``device`` / ``port`` are the real CML endpoint (used to find the L1
    peer). ``draw_device`` / ``draw_port`` are what the diagram links: the
    same port for an Edge or a PE, a fresh ``Dummy N`` on the VRF-GW for an
    EVPN border leaf (``draw_port`` None until first used).

    ``members`` lists the physical ports of a ``Bundle-Ether`` /
    ``Port-channel`` attachment: the CML cable is on a member, while the IP
    and VRF stay on the logical port, as in the underlay.
    """
    device: str
    port: str
    overlay: str            # 'evpn' | 'sdwan' | 'l3vpn'
    context: str            # 'VPN 10' / 'VRF BLUE'
    vrf: Optional[str]
    cidrs: List[str]
    draw_device: str
    draw_port: Optional[str] = None
    lan_dummy: bool = False  # SD-WAN: an unlinked LAN port goes to a LAN dummy
    members: List[str] = field(default_factory=list)

    def cml_ports(self) -> List[str]:
        """The ports whose CML links belong to this attachment."""
        return list(self.members) or [self.port]

    def end(self, canvas: "OverlayCanvas") -> Tuple[str, str]:
        """The drawn (addressed) endpoint; its IP and VRF are set the first time."""
        if self.draw_port is None:
            self.draw_port = canvas.dummy(self.draw_device)
        if (self.draw_device, self.draw_port) not in canvas.addressed:
            canvas.addressed.add((self.draw_device, self.draw_port))
            canvas.ip(self.draw_device, self.draw_port, self.cidrs)
            canvas.vrf(self.draw_device, self.draw_port, self.vrf)
        return self.draw_device, self.draw_port

    def link_end(self, canvas: "OverlayCanvas", cml_port: str) -> Tuple[str, str]:
        """The port a link to the peer of ``cml_port`` is drawn on.

        For a LAG on a real device that is the member port, which joins the
        port-channel. A logical end (the VRF-GW) has one port, so only its
        first member is linked.
        """
        end = self.end(canvas)
        if self.members and self.draw_device == self.device:
            canvas.portchannel(self.device, cml_port, self.port)
            return self.device, cml_port
        return end


@dataclass
class OverlayCanvas:
    model: NSModel = field(default_factory=NSModel)
    lab_of_area: Dict[str, str] = field(default_factory=dict)
    rows: Dict[str, Dict[int, List[str]]] = field(default_factory=lambda: defaultdict(lambda: defaultdict(list)))
    next_dummy: Dict[str, int] = field(default_factory=lambda: defaultdict(lambda: 1))
    linked_ports: Set[Tuple[str, str]] = field(default_factory=set)
    addressed: Set[Tuple[str, str]] = field(default_factory=set)
    handoffs: List[Handoff] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    counts: Dict[str, int] = field(default_factory=lambda: defaultdict(int))
    underlay_devices: Set[str] = field(default_factory=set)   # drawn as in the underlay
    fixed_grids: Dict[str, List[List[str]]] = field(default_factory=dict)  # area -> underlay grid
    _names: Set[str] = field(default_factory=set)
    _ip_ports: Set[Tuple[str, str]] = field(default_factory=set)
    _vrf_ports: Set[Tuple[str, str]] = field(default_factory=set)
    _l2_ports: Set[Tuple[str, str, str]] = field(default_factory=set)
    _crossed: Set[frozenset] = field(default_factory=set)

    def note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)

    def note_cross(self, why: str, a: Tuple[str, str], b: Tuple[str, str]) -> None:
        """Report, once per link, a link not drawn because it would cross areas."""
        key = frozenset((a, b))
        if key in self._crossed:
            return
        self._crossed.add(key)
        self.note(f"Not drawn ({why}): {a[0]} {a[1]} [{self.area_of(a[0])}] -- "
                  f"{b[0]} {b[1]} [{self.area_of(b[0])}] would cross areas (RULE 3).")

    # ----- devices -----
    def has(self, name: str) -> bool:
        return name in self.model.devices

    def area_of(self, name: str) -> Optional[str]:
        d = self.model.devices.get(name)
        return d.area if d else None

    def unique_name(self, base: str) -> str:
        """``base`` (or ``base_2`` …) not yet used, compared without case."""
        name, n = base, 2
        while name.casefold() in self._names:
            name = f"{base}_{n}"
            n += 1
        return name

    def add_device(
        self,
        name: str,
        area: str,
        lab: str,
        row: int,
        stencil: StencilMapping,
        color: Optional[Tuple[int, int, int]] = None,
        attribute: str = "",
        is_endpoint: bool = False,
    ) -> NSDevice:
        """Place ``name`` once; a second call returns the existing device."""
        if name in self.model.devices:
            return self.model.devices[name]
        device = NSDevice(
            name=name, area=area, row=row, stencil=stencil,
            is_endpoint=is_endpoint, routing_attribute=attribute,
            default_color=color, attribute_prefix="Overlay",
        )
        self.model.devices[name] = device
        self._names.add(name.casefold())
        self.lab_of_area.setdefault(area, lab)
        self.rows[area][row].append(name)
        self.counts["devices"] += 1
        return device

    def add_underlay_device(self, device: NSDevice, area: str, lab: str, row: int) -> NSDevice:
        """Place an underlay device as it is (stencil, colour, Routing attribute)."""
        if device.name in self.model.devices:
            return self.model.devices[device.name]
        copy = replace(device, area=area, row=row)
        self.model.devices[device.name] = copy
        self._names.add(device.name.casefold())
        self.lab_of_area.setdefault(area, lab)
        self.rows[area][row].append(device.name)
        self.underlay_devices.add(device.name)
        self.counts["underlay_devices"] += 1
        return copy

    def add_handoff_note(self, device: str, text: str) -> None:
        d = self.model.devices.get(device)
        if d is None:
            return
        d.attribute_e = f"{d.attribute_e} | {text}" if d.attribute_e else f"Handoff: {text}"

    # ----- ports -----
    def dummy(self, device: str) -> str:
        """Next free ``Dummy N`` port (N >= 1) of a drawing device."""
        n = self.next_dummy[device]
        self.next_dummy[device] = n + 1
        return f"Dummy {n}"

    def port_free(self, device: str, port: str) -> bool:
        return (device, port) not in self.linked_ports

    def crosses_areas(self, a_device: str, b_device: str) -> bool:
        """True when a link would join two different non-waypoint areas (NS RULE 3)."""
        area_a, area_b = self.area_of(a_device), self.area_of(b_device)
        return (area_a != area_b and not (area_a or "").endswith("_wp_")
                and not (area_b or "").endswith("_wp_"))

    def link(self, a_device: str, a_port: str, b_device: str, b_port: str) -> bool:
        """Add an L1 link unless either port already has one.

        A link between two different non-waypoint areas is refused (NS RULE 3)
        and returns False so the caller can report it.
        """
        if not self.port_free(a_device, a_port) or not self.port_free(b_device, b_port):
            return False
        if self.crosses_areas(a_device, b_device):
            return False
        self.model.l1_links.append(NSL1Link(a_device, a_port, b_device, b_port))
        self.linked_ports.update({(a_device, a_port), (b_device, b_port)})
        self.counts["l1_links"] += 1
        return True

    def virtual_port(self, device: str, port: str, is_loopback: bool = False, vlan_id: Optional[int] = None) -> None:
        self.model.virtual_ports.append(NSVirtualPort(device, port, is_loopback, vlan_id))
        self.counts["virtual_ports"] += 1

    def ip(self, device: str, port: str, cidrs: List[str]) -> None:
        """Set a port's addresses once; a later call for the same port is ignored."""
        if cidrs and (device, port) not in self._ip_ports:
            self._ip_ports.add((device, port))
            self.model.ip_assignments.append(NSIPAssignment(device, port, list(cidrs)))
            self.counts["ip_assignments"] += 1

    def vrf(self, device: str, port: str, vrf: Optional[str]) -> None:
        if vrf and (device, port) not in self._vrf_ports:
            self._vrf_ports.add((device, port))
            self.model.vrf_renames.append((device, port, vrf))
            self.counts["vrf_renames"] += 1

    def l2(self, device: str, port: str, segment: str) -> None:
        self.l2_many(device, port, [segment])

    def l2_many(self, device: str, port: str, segments: List[str]) -> None:
        new = [s for s in segments if (device, port, s) not in self._l2_ports]
        if new:
            self._l2_ports.update((device, port, s) for s in new)
            self.model.l2_segments_phys.append(NSL2Segment(device, port, new))

    def l2_svi(self, device: str, port: str, segment: str) -> None:
        self.model.l2_segments_svi.append(NSL2Segment(device, port, [segment]))

    def portchannel(self, device: str, member: str, logical: str) -> None:
        """Add ``member`` to ``device``'s port-channel ``logical``."""
        for pc in self.model.port_channels:
            if pc.device == device and pc.portchannel_name == logical:
                if member not in pc.physical_ports:
                    pc.physical_ports.append(member)
                return
        self.model.port_channels.append(NSPortChannel(device, [member], logical))

    def seed_names(self, names: List[str]) -> None:
        """Reserve the CML labels so a drawing device never reuses one."""
        self._names.update(n.casefold() for n in names if n)

    def prune_missing_ports(self) -> int:
        """Drop IP / VRF / L2 entries on ports that no link or virtual port made.

        NS rejects a whole bulk command for one row on a port that does not
        exist, so an address left behind by a refused link must go.
        """
        m = self.model
        pcs: List[NSPortChannel] = []
        for pc in m.port_channels:
            pc.physical_ports = sorted(p for p in set(pc.physical_ports) if (pc.device, p) in self.linked_ports)
            if pc.physical_ports:
                pcs.append(pc)
        m.port_channels = pcs
        m.subinterfaces = [s for s in m.subinterfaces if (s.device, s.parent_port) in self.linked_ports]
        exists = set(self.linked_ports) | {(v.device, v.port) for v in m.virtual_ports}
        exists.update((pc.device, pc.portchannel_name) for pc in m.port_channels)
        exists.update((s.device, s.subif_port) for s in m.subinterfaces)
        # A port-channel member carries no address of its own.
        members = {(pc.device, p) for pc in m.port_channels for p in pc.physical_ports}
        exists -= members
        before = len(m.ip_assignments) + len(m.vrf_renames) + len(m.l2_segments_phys) + len(m.l2_segments_svi)
        m.ip_assignments = [x for x in m.ip_assignments if (x.device, x.port) in exists]
        m.vrf_renames = [x for x in m.vrf_renames if (x[0], x[1]) in exists]
        m.l2_segments_phys = [x for x in m.l2_segments_phys if (x.device, x.port) in exists]
        m.l2_segments_svi = [x for x in m.l2_segments_svi if (x.device, x.port) in exists]
        after = len(m.ip_assignments) + len(m.vrf_renames) + len(m.l2_segments_phys) + len(m.l2_segments_svi)
        return before - after
