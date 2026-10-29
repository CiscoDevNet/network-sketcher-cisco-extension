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
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from .stencil_mapper import StencilMapping
from .topology_mapper import (
    NSDevice, NSIPAssignment, NSL1Link, NSL2Segment, NSModel, NSVirtualPort,
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

    def end(self, canvas: "OverlayCanvas") -> Tuple[str, str]:
        """The drawn endpoint; its IP and VRF are set the first time."""
        if self.draw_port is None:
            self.draw_port = canvas.dummy(self.draw_device)
        if (self.draw_device, self.draw_port) not in canvas.addressed:
            canvas.addressed.add((self.draw_device, self.draw_port))
            canvas.ip(self.draw_device, self.draw_port, self.cidrs)
            canvas.vrf(self.draw_device, self.draw_port, self.vrf)
        return self.draw_device, self.draw_port


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
    _names: Set[str] = field(default_factory=set)

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

    def link(self, a_device: str, a_port: str, b_device: str, b_port: str) -> bool:
        """Add an L1 link unless either port already has one.

        A link between two different non-waypoint areas is refused (NS RULE 3)
        and returns False so the caller can report it.
        """
        if not self.port_free(a_device, a_port) or not self.port_free(b_device, b_port):
            return False
        area_a, area_b = self.area_of(a_device), self.area_of(b_device)
        if area_a != area_b and not (area_a or "").endswith("_wp_") and not (area_b or "").endswith("_wp_"):
            return False
        self.model.l1_links.append(NSL1Link(a_device, a_port, b_device, b_port))
        self.linked_ports.update({(a_device, a_port), (b_device, b_port)})
        self.counts["l1_links"] += 1
        return True

    def virtual_port(self, device: str, port: str, is_loopback: bool = False, vlan_id: Optional[int] = None) -> None:
        self.model.virtual_ports.append(NSVirtualPort(device, port, is_loopback, vlan_id))
        self.counts["virtual_ports"] += 1

    def ip(self, device: str, port: str, cidrs: List[str]) -> None:
        if cidrs:
            self.model.ip_assignments.append(NSIPAssignment(device, port, list(cidrs)))
            self.counts["ip_assignments"] += 1

    def vrf(self, device: str, port: str, vrf: Optional[str]) -> None:
        if vrf:
            self.model.vrf_renames.append((device, port, vrf))
            self.counts["vrf_renames"] += 1

    def l2(self, device: str, port: str, segment: str) -> None:
        self.model.l2_segments_phys.append(NSL2Segment(device, port, [segment]))

    def l2_svi(self, device: str, port: str, segment: str) -> None:
        self.model.l2_segments_svi.append(NSL2Segment(device, port, [segment]))

    def seed_names(self, names: List[str]) -> None:
        """Reserve the CML labels so a drawing device never reuses one."""
        self._names.update(n.casefold() for n in names if n)

    def prune_missing_ports(self) -> int:
        """Drop IP / VRF / L2 entries on ports that no link or virtual port made.

        NS rejects a whole bulk command for one row on a port that does not
        exist, so an address left behind by a refused link must go.
        """
        exists = set(self.linked_ports) | {(v.device, v.port) for v in self.model.virtual_ports}
        m = self.model
        before = len(m.ip_assignments) + len(m.vrf_renames) + len(m.l2_segments_phys) + len(m.l2_segments_svi)
        m.ip_assignments = [x for x in m.ip_assignments if (x.device, x.port) in exists]
        m.vrf_renames = [x for x in m.vrf_renames if (x[0], x[1]) in exists]
        m.l2_segments_phys = [x for x in m.l2_segments_phys if (x.device, x.port) in exists]
        m.l2_segments_svi = [x for x in m.l2_segments_svi if (x.device, x.port) in exists]
        after = len(m.ip_assignments) + len(m.vrf_renames) + len(m.l2_segments_phys) + len(m.l2_segments_svi)
        return before - after
