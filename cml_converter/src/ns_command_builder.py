# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""Serialise NSModel → Network Sketcher CLI command strings (RULE 7 order).

Output is a list of `(phase, command_string)` tuples; writing them in order
produces a Phase 1→6 compliant script ready for `run_commands`.

Key NS conventions implemented here:
- All commands except `rename attribute_bulk` use single-quoted strings inside
  the outer double-quoted argument: `"[['DEV','Port',[...]]]"`.
- `rename attribute_bulk` uses the two-level escaped-double-quote format
  documented in RULE 16 (see ns_engine/nsm_extensions_cmd_list.txt L1052-).
- Port names always have a single space between the type and the number
  (e.g. `Ethernet 1/1`, `Vlan 100`, `Port-channel 10`). This is enforced in
  topology_mapper.normalise_port_name().
- L1 link side ports are de-duplicated; CML may list a single link twice if
  both endpoints publish it.
- Endpoint devices (`is_endpoint=True`) are NEVER given an SVI; their IP goes
  directly onto the L1 physical port (RULE 11.5).
- Every SVI added in Phase 3 also receives a `add l2_segment_bulk` self-bind
  entry on the SVI itself (RULE 15).
- Port-channel members and the port-channel virtual interface itself follow
  RULE 9 (VLAN symmetry maintained from the parsed config).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .topology_mapper import (
    NSDevice, NSL1Link, NSL2Segment, NSIPAssignment,
    NSModel, NSPortChannel, NSSubInterface, NSVirtualPort, normalise_port_name,
)
from .stencil_mapper import NS_CLOUD, NS_PC, NS_PHONE, NS_SERVER, StencilMapping


# RULE 16 colour palette (matches the reference examples in the AI context).
# Attribute-sheet 'Default' cell colours, role-based and unified with
# cv_converter / sna_converter. Every CML device is observed (real), so the
# light-gray "inferred / does not exist" colour is reserved for WAN/Internet/
# cloud waypoints only; real gear/servers/clients get their role colour.
_COLOR_NET = (235, 241, 222)       # light green  — real network gear
_COLOR_SERVER = (255, 204, 204)    # light red    — servers
_COLOR_PC = (255, 255, 204)        # light yellow — client PCs / phones
_COLOR_WAYPOINT = (200, 200, 200)  # light gray   — WAN/Internet/cloud waypoint (no real device)
_COLOR_MODEL = (255, 183, 219)
_COLOR_OS = (200, 230, 255)
_COLOR_STENCIL = (220, 230, 242)
_COLOR_WHITE = (255, 255, 255)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _q_list(items: Sequence[str]) -> str:
    """Render a Python list literal using single quotes (NS convention)."""
    return "[" + ",".join(f"'{i}'" for i in items) + "]"


def _q_outer(payload: str) -> str:
    """Wrap a NS bulk-command argument in a double-quoted shell-safe form."""
    return f'"{payload}"'


def _attr_cell(value: str, rgb: tuple = _COLOR_WHITE) -> str:
    r, g, b = rgb
    safe = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"\\\"['{safe}',[{r}, {g}, {b}]]\\\""


# ---------------------------------------------------------------------------
# Phase 1: areas + device placement
# ---------------------------------------------------------------------------

def cmd_add_area_location(model: NSModel) -> str:
    """Phase 1 step 1: add area_location."""
    layout = "[" + ",".join(_q_list(row) for row in model.areas) + "]"
    return f'add area_location "{layout}"'


def cmd_add_device_location_per_area(model: NSModel) -> List[str]:
    """Phase 1 step 2: add device_location per area (batch grid)."""
    out: List[str] = []
    for area, grid in model.area_to_devices.items():
        if not grid:
            continue
        rendered_grid = "[" + ",".join(_q_list(row) for row in grid) + "]"
        out.append(f"add device_location \"['{area}',{rendered_grid}]\"")
    return out


# ---------------------------------------------------------------------------
# Phase 2 + 2.5: L1 links + port-info
# ---------------------------------------------------------------------------

def cmd_add_l1_link_bulk(model: NSModel) -> Optional[str]:
    """Phase 2: add l1_link_bulk (one bulk call for all CML links)."""
    if not model.l1_links:
        return None
    rows: List[str] = []
    seen: set = set()
    for lk in model.l1_links:
        # Skip self-loops or duplicates.
        if lk.a_device == lk.b_device:
            continue
        key = frozenset({(lk.a_device, lk.a_port), (lk.b_device, lk.b_port)})
        if key in seen:
            continue
        seen.add(key)
        rows.append(f"['{lk.a_device}','{lk.b_device}','{lk.a_port}','{lk.b_port}']")
    if not rows:
        return None
    payload = "[" + ",".join(rows) + "]"
    return f"add l1_link_bulk \"{payload}\""


def cmd_rename_port_info_bulk(model: NSModel) -> Optional[str]:
    """Phase 2.5: set Speed/Duplex/Port_Type for all L1 ports of every device.

    POC defaults: 10Gbps full 10GBASE-SR for L3Switches, 1Gbps full 1000BASE-T
    for everything else. Endpoints get 1Gbps as well. Uses `_ALL_` per device.
    """
    if not model.devices:
        return None
    groups: Dict[Tuple[str, str, str], List[str]] = {}
    for d in model.devices.values():
        if d.stencil.stencil_type == NS_CLOUD:
            continue
        if d.stencil.stencil_type == "L3Switch":
            spec = ("10Gbps", "Full", "10GBASE-SR")
        elif d.stencil.stencil_type == "Router":
            spec = ("1Gbps", "Full", "1000BASE-T")
        else:
            spec = ("1Gbps", "Full", "1000BASE-T")
        groups.setdefault(spec, []).append(d.name)

    entries: List[str] = []
    for (speed, duplex, ptype), names in groups.items():
        if not names:
            continue
        dev_field = _q_list(sorted(names)) if len(names) > 1 else f"'{names[0]}'"
        entries.append(f"[{dev_field},'_ALL_',['{speed}','{duplex}','{ptype}']]")
    if not entries:
        return None
    payload = "[" + ",".join(entries) + "]"
    return f"rename port_info_bulk \"{payload}\""


# ---------------------------------------------------------------------------
# Phase 3: portchannel + virtual_port + l2_segment
# ---------------------------------------------------------------------------

def cmd_add_vport_l1if_direct_binding(model: NSModel) -> List[str]:
    """Phase 3: create dot1q sub-interfaces bound to their parent L1 port.

    NS has no bulk form, so we emit one command per sub-interface. This MUST
    run after Phase 2 (l1_link, which creates the parent port) and before
    Phase 4 (ip_address_bulk), otherwise the IP is rejected with
    "L3 interface not found".
    """
    out: List[str] = []
    seen: set = set()
    for si in model.subinterfaces:
        key = (si.device, si.subif_port)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            f"add vport_l1if_direct_binding '{si.device}' '{si.parent_port}' '{si.subif_port}'"
        )
    return out


def cmd_add_vport_l2_direct_binding(model: NSModel) -> List[str]:
    """Phase 3: bind the dot1q VLAN to each sub-interface (only when known).

    Note: l2_segment and vport_l2_direct_binding cannot coexist on the same
    virtual port, so sub-interfaces never receive an l2_segment entry.
    """
    out: List[str] = []
    seen: set = set()
    for si in model.subinterfaces:
        if si.vlan_id is None:
            continue
        key = (si.device, si.subif_port)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            f"add vport_l2_direct_binding '{si.device}' '{si.subif_port}' vlan{si.vlan_id}"
        )
    return out


def cmd_add_portchannel_bulk(model: NSModel) -> Optional[str]:
    """Phase 3 step 5: add portchannel_bulk."""
    if not model.port_channels:
        return None
    rows: List[str] = []
    for pc in model.port_channels:
        ports = _q_list(pc.physical_ports)
        rows.append(f"['{pc.device}',{ports},'{pc.portchannel_name}']")
    payload = "[" + ",".join(rows) + "]"
    return f"add portchannel_bulk \"{payload}\""


def cmd_add_virtual_port_bulk(model: NSModel) -> Optional[str]:
    """Phase 3 step 6: add virtual_port_bulk (SVIs + Loopbacks)."""
    if not model.virtual_ports:
        return None
    by_device: Dict[str, List[str]] = {}
    for vp in model.virtual_ports:
        by_device.setdefault(vp.device, []).append(vp.port)
    rows = [f"['{d}',{_q_list(sorted(set(ps)))}]" for d, ps in sorted(by_device.items())]
    payload = "[" + ",".join(rows) + "]"
    return f"add virtual_port_bulk \"{payload}\""


def cmd_add_l2_segment_bulk(model: NSModel) -> Optional[str]:
    """Phase 3 step 8: add l2_segment_bulk (physical trunks + SVI self-binds)."""
    rows: List[str] = []
    for seg in model.l2_segments_phys:
        vlans = _q_list(seg.vlans)
        rows.append(f"['{seg.device}','{seg.port}',{vlans}]")
    for seg in model.l2_segments_svi:
        vlans = _q_list(seg.vlans)
        rows.append(f"['{seg.device}','{seg.port}',{vlans}]")
    if not rows:
        return None
    payload = "[" + ",".join(rows) + "]"
    return f"add l2_segment_bulk \"{payload}\""


# ---------------------------------------------------------------------------
# Phase 4: IP addresses + VRFs
# ---------------------------------------------------------------------------

def cmd_add_ip_address_bulk(model: NSModel) -> Optional[str]:
    """Phase 4 step 10: add ip_address_bulk."""
    if not model.ip_assignments:
        return None
    rows: List[str] = []
    for ip in model.ip_assignments:
        cidrs = _q_list(ip.cidrs)
        rows.append(f"['{ip.device}','{ip.port}',{cidrs}]")
    payload = "[" + ",".join(rows) + "]"
    return f"add ip_address_bulk \"{payload}\""


def cmd_rename_l3_instance(model: NSModel) -> List[str]:
    """Phase 4 step 11: per-port rename l3_instance (NS has no bulk form)."""
    out: List[str] = []
    for device, port, vrf in model.vrf_renames:
        out.append(f"rename l3_instance '{device}' '{port}' '{vrf}'")
    return out


# ---------------------------------------------------------------------------
# Phase 6: attribute_bulk (Stencil Type, Model, OS, routing summary)
# ---------------------------------------------------------------------------

def cmd_rename_attribute_bulk(model: NSModel) -> str:
    """Phase 6 step 13: rename attribute_bulk (single bulk, sets header + rows).

    Column layout:
      0 Device Name
      1 Default      (role-based colour: green=net gear, red=Server, yellow=PC/Phone;
                      'WayPoint' light-gray for WAN/Internet/cloud waypoints)
      2 Model        (long human description)
      3 OS           (NX-OS / IOS-XE / IOS / SD-WAN / ...)
      4 Stencil Type (RULE 16: Router / L3Switch / Switch / Firewall / WLC / AP / Server / Cloud / PC)
      5 Attribute-D  ('Routing: <BGP/OSPF/EVPN snapshot>' or '')
      6 Attribute-E
      7 Attribute-F
      8 Attribute-G
      9 Attribute-H
    """
    header = (
        "['Device Name','Default','Model','OS','Stencil Type',"
        "'Attribute-D','Attribute-E','Attribute-F','Attribute-G','Attribute-H']"
    )
    rows: List[str] = []
    for name, d in sorted(model.devices.items()):
        is_waypoint_area = d.area.endswith("_wp_") or d.stencil.stencil_type == NS_CLOUD
        if is_waypoint_area:
            token, rgb = "WayPoint", _COLOR_WAYPOINT
        elif d.stencil.stencil_type == NS_SERVER:
            token, rgb = "DEVICE", _COLOR_SERVER
        elif d.stencil.stencil_type in (NS_PC, NS_PHONE):
            token, rgb = "DEVICE", _COLOR_PC
        else:  # Router / L3Switch / Switch / Firewall / WLC / AP
            token, rgb = "DEVICE", _COLOR_NET
        # A per-device override (e.g. an OBSERVED external_connector waypoint,
        # which is a real bridge device rather than an inferred WAN cloud)
        # wins over the role-based colour; the token (DEVICE / WayPoint) is
        # left to the stencil.
        if getattr(d, "default_color", None):
            rgb = d.default_color
        default_cell = _attr_cell(token, rgb)
        model_cell = _attr_cell(d.stencil.model, _COLOR_MODEL)
        os_cell = _attr_cell(d.stencil.os, _COLOR_OS)
        stencil_cell = _attr_cell(d.stencil.stencil_type, _COLOR_STENCIL)

        # Routing summary truncated to ~120 chars to keep the bulk command sane.
        routing_summary = (d.routing_attribute or "").strip().replace("\n", " | ")
        routing_summary = routing_summary[:120]
        attr_d_cell = _attr_cell(f"Routing: {routing_summary}" if routing_summary else "", _COLOR_WHITE)

        # Empties (omitted -> auto-completed white per RULE 16 documentation).
        rows.append(
            f"['{name}', {default_cell}, {model_cell}, {os_cell}, {stencil_cell}, {attr_d_cell}]"
        )

    payload = "[" + ",".join(rows + [header]) + "]"
    # Outer wrap with double-quotes (escapes are already inside).
    return f"rename attribute_bulk \"{payload}\""


# ---------------------------------------------------------------------------
# Top-level orchestration
# ---------------------------------------------------------------------------

@dataclass
class CommandScript:
    lines: List[str]
    counts: Dict[str, int]

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


def build_command_script(model: NSModel) -> CommandScript:
    lines: List[str] = []
    counts: Dict[str, int] = {}

    def _add(label: str, cmd: Optional[str]) -> None:
        if not cmd:
            return
        lines.append(f"# Phase: {label}")
        lines.append(cmd)
        lines.append("")
        counts[label] = counts.get(label, 0) + 1

    def _add_many(label: str, cmds: Iterable[str]) -> None:
        added = 0
        for c in cmds:
            lines.append(f"# Phase: {label}")
            lines.append(c)
            lines.append("")
            added += 1
        if added:
            counts[label] = counts.get(label, 0) + added

    # Phase 1
    _add("1 area_location", cmd_add_area_location(model))
    _add_many("1 device_location", cmd_add_device_location_per_area(model))

    # Phase 2 + 2.5
    _add("2 l1_link_bulk", cmd_add_l1_link_bulk(model))
    _add("2.5 port_info_bulk", cmd_rename_port_info_bulk(model))

    # Phase 3 (sub-if bindings -> portchannel -> virtual_port -> l2_segment).
    # Sub-interface bindings run first so their parent L1 ports (from Phase 2)
    # exist and the sub-ifs are ready before Phase 4 assigns their IPs.
    _add_many("3 vport_l1if_direct_binding", cmd_add_vport_l1if_direct_binding(model))
    _add_many("3 vport_l2_direct_binding", cmd_add_vport_l2_direct_binding(model))
    _add("3 portchannel_bulk", cmd_add_portchannel_bulk(model))
    _add("3 virtual_port_bulk", cmd_add_virtual_port_bulk(model))
    _add("3 l2_segment_bulk", cmd_add_l2_segment_bulk(model))

    # Phase 4 (IP -> VRF)
    _add("4 ip_address_bulk", cmd_add_ip_address_bulk(model))
    _add_many("4 l3_instance (VRF)", cmd_rename_l3_instance(model))

    # Phase 6 (attribute_bulk)
    _add("6 attribute_bulk", cmd_rename_attribute_bulk(model))

    return CommandScript(lines=lines, counts=counts)
