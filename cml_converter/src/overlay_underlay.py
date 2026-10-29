# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0
"""Copy underlay devices and ports into the integrated overlay.

The overlay script shows the overlay only where there is one. A lab with no
overlay is drawn exactly as in the underlay, and the devices of an overlay
lab that take no part in the overlay (P routers, controllers, transport
switches) are drawn as in the underlay next to it. Everything here is read
from the underlay ``NSModel`` that ``build_ns_model`` already pruned, so a
copied port carries the same IP, VRF, L2 segment, port-channel and
sub-interface as in the underlay script.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from typing import Dict, List, Tuple

from .overlay_canvas import OverlayCanvas
from .topology_mapper import NSModel, NSPortChannel, NSSubInterface, NSVirtualPort

Port = Tuple[str, str]


class UnderlayCopier:
    """Per-device and per-port views of the underlay model."""

    def __init__(self, canvas: OverlayCanvas, underlay: NSModel) -> None:
        self.canvas = canvas
        self.underlay = underlay
        self.virtual_ports: Dict[str, List[NSVirtualPort]] = defaultdict(list)
        self.ips: Dict[Port, List[str]] = {}
        self.vrfs: Dict[Port, str] = {}
        self.l2: Dict[Port, List[str]] = defaultdict(list)
        self.l2_svi: Dict[Port, List[str]] = defaultdict(list)
        self.port_channels: Dict[str, List[NSPortChannel]] = defaultdict(list)
        self.pc_of_member: Dict[Port, NSPortChannel] = {}
        self.subifs: Dict[str, List[NSSubInterface]] = defaultdict(list)
        self.subifs_of_parent: Dict[Port, List[NSSubInterface]] = defaultdict(list)
        for v in underlay.virtual_ports:
            self.virtual_ports[v.device].append(v)
        for ip in underlay.ip_assignments:
            self.ips[(ip.device, ip.port)] = list(ip.cidrs)
        for device, port, vrf in underlay.vrf_renames:
            self.vrfs[(device, port)] = vrf
        for seg in underlay.l2_segments_phys:
            self.l2[(seg.device, seg.port)].extend(seg.vlans)
        for seg in underlay.l2_segments_svi:
            self.l2_svi[(seg.device, seg.port)].extend(seg.vlans)
        for pc in underlay.port_channels:
            self.port_channels[pc.device].append(pc)
            for member in pc.physical_ports:
                self.pc_of_member[(pc.device, member)] = pc
        for si in underlay.subinterfaces:
            self.subifs[si.device].append(si)
            self.subifs_of_parent[(si.device, si.parent_port)].append(si)
        self.ports_of: Dict[str, List[str]] = defaultdict(list)
        for key in list(self.ips) + list(self.vrfs) + list(self.l2) + list(self.l2_svi):
            if key[1] not in self.ports_of[key[0]]:
                self.ports_of[key[0]].append(key[1])

    def _port_items(self, device: str, port: str) -> None:
        c = self.canvas
        c.ip(device, port, self.ips.get((device, port), []))
        c.vrf(device, port, self.vrfs.get((device, port)))
        if self.l2.get((device, port)):
            c.l2_many(device, port, self.l2[(device, port)])
        for vlan in self.l2_svi.get((device, port), []):
            c.l2_svi(device, port, vlan)

    def copy_device(self, name: str, area: str, lab: str, row: int) -> None:
        """Draw ``name`` with every port item it has in the underlay."""
        c = self.canvas
        c.add_underlay_device(self.underlay.devices[name], area, lab, row)
        for v in self.virtual_ports.get(name, []):
            c.virtual_port(v.device, v.port, v.is_loopback, v.vlan_id)
        for pc in self.port_channels.get(name, []):
            for member in pc.physical_ports:
                c.portchannel(name, member, pc.portchannel_name)
        for si in self.subifs.get(name, []):
            c.model.subinterfaces.append(replace(si))
        for port in self.ports_of.get(name, []):
            self._port_items(name, port)

    def copy_port(self, device: str, port: str) -> None:
        """Bring the underlay items of one port of an overlay device along.

        Used when an underlay link to that port is drawn: its IP / VRF / L2,
        the port-channel it is a member of (with the logical port's items),
        and the sub-interfaces it parents.
        """
        c = self.canvas
        self._port_items(device, port)
        pc = self.pc_of_member.get((device, port))
        if pc is not None:
            c.portchannel(device, port, pc.portchannel_name)
            self._port_items(device, pc.portchannel_name)
        for si in self.subifs_of_parent.get((device, port), []):
            if not any(s.device == si.device and s.subif_port == si.subif_port for s in c.model.subinterfaces):
                c.model.subinterfaces.append(replace(si))
                self._port_items(device, si.subif_port)
