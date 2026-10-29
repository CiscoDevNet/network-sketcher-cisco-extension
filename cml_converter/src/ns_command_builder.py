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

import ipaddress
import json
from dataclasses import dataclass, field
from typing import AbstractSet, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from .topology_mapper import (
    NSDevice, NSL1Link, NSL2Segment, NSIPAssignment,
    NSModel, NSPortChannel, NSSubInterface, NSVirtualPort, normalise_port_name,
)
from .stencil_mapper import (
    NS_CLOUD, NS_PC, NS_PHONE, NS_SERVER, StencilMapping,
    model_is_external_connector,
)


# RULE 16 colour palette (matches the reference examples in the AI context).
# Attribute-sheet 'Default' cell colours, role-based and unified with
# cv_converter / sna_converter. Every CML device is observed (real), so the
# light-gray "inferred / does not exist" colour is reserved for WAN/Internet/
# cloud waypoints only; real gear/servers/clients get their role colour.
_COLOR_NET = (235, 241, 222)       # light green  — real network gear
_COLOR_SERVER = (255, 204, 204)    # light red    — servers
_COLOR_PC = (255, 255, 204)        # light yellow — client PCs / phones
_OBSERVED_WAYPOINT_BLUE = (220, 230, 242)  # light blue — External Connector waypoint
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
    # NS re-parses each cell as a single-quoted Python literal, but shlex has
    # already consumed one level of escaping by then, so a "\'" arrives as a
    # bare "'" and breaks the parse. Drop apostrophes instead of escaping them.
    safe = value.replace("\\", "\\\\").replace("'", "")
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


def cmd_rename_port_info_bulk(
    model: NSModel, unknown: Union[bool, AbstractSet[str]] = False,
) -> Optional[str]:
    """Phase 2.5: set Speed/Duplex/Port_Type for all L1 ports of every device.

    POC defaults: 10Gbps full 10GBASE-SR for L3Switches, 1Gbps full 1000BASE-T
    for everything else. Endpoints get 1Gbps as well. Uses `_ALL_` per device.
    With ``unknown`` (the overlay, whose links are logical) every port is
    Unknown/Unknown/Unknown; a set of names limits that to those devices.
    """
    if not model.devices:
        return None
    groups: Dict[Tuple[str, str, str], List[str]] = {}
    for d in model.devices.values():
        if d.stencil.stencil_type == NS_CLOUD:
            continue
        if unknown is True or (not isinstance(unknown, bool) and d.name in unknown):
            spec = ("Unknown", "Unknown", "Unknown")
        elif d.stencil.stencil_type == "L3Switch":
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
        # 'Vlan<id>' (not 'vlan<id>') so it matches the switch-side segment,
        # quoted because the MCP command validator requires it.
        name = f"Vlan{si.vlan_id}" + ("_UNTAG_" if getattr(si, "native", False) else "")
        out.append(
            f"add vport_l2_direct_binding '{si.device}' '{si.subif_port}' '{name}'"
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

def ns_ip_cidr(raw: str) -> Tuple[Optional[str], str]:
    """Return ``(cidr, reason)`` for one address. IPv4 passes through unchanged.

    Network Sketcher rejects IPv6 without a prefix length, with a zone index,
    or of the link-local, multicast, loopback, unspecified, IPv4-mapped or
    site-local kinds; those return ``(None, reason)``. Accepted IPv6 is
    written in lower-case compressed form.
    """
    text = (raw or "").strip()
    if ":" not in text:
        return raw, ""
    if "%" in text:
        return None, "zone index"
    if "/" not in text:
        return None, "no prefix length"
    try:
        iface = ipaddress.IPv6Interface(text)
    except ValueError:
        return None, "invalid IPv6"
    addr = iface.ip
    if addr.is_link_local:
        return None, "link-local"
    if addr.is_multicast:
        return None, "multicast"
    if addr.is_loopback:
        return None, "loopback"
    if addr.is_unspecified:
        return None, "unspecified"
    if addr.ipv4_mapped is not None:
        return None, "IPv4-mapped"
    if addr.is_site_local:
        return None, "site-local"
    return iface.with_prefixlen, ""


def cmd_add_ip_address_bulk(
    model: NSModel, dropped: Optional[List[Tuple[str, str, str, str]]] = None,
) -> Optional[str]:
    """Phase 4 step 10: add ip_address_bulk.

    Addresses ``ns_ip_cidr`` rejects are left out and appended to ``dropped``
    as ``(device, port, address, reason)``. A row whose every address is left
    out is not written.
    """
    if not model.ip_assignments:
        return None
    rows: List[str] = []
    for ip in model.ip_assignments:
        kept: List[str] = []
        for raw in ip.cidrs:
            cidr, reason = ns_ip_cidr(raw)
            if cidr is None:
                if dropped is not None:
                    dropped.append((ip.device, ip.port, raw, reason))
                continue
            if ":" in cidr and cidr in kept:
                continue
            kept.append(cidr)
        if ip.cidrs and not kept:
            continue
        cidrs = _q_list(kept)
        rows.append(f"['{ip.device}','{ip.port}',{cidrs}]")
    if not rows:
        return None
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
                      'WayPoint' light blue only when Model contains External Connector)
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
        # Waypoint is the Model text, not the area name. A router that landed
        # in a wan-labelled area is still a normal device. The overlay's
        # VPN / VRF clouds are Cloud stencils placed in a ``*_wp_`` area.
        if model_is_external_connector(d.stencil.model) or (
            d.stencil.stencil_type == NS_CLOUD and d.area.endswith("_wp_")
        ):
            token, rgb = "WayPoint", _OBSERVED_WAYPOINT_BLUE
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
        prefix = getattr(d, "attribute_prefix", "Routing") or "Routing"
        attr_d_cell = _attr_cell(f"{prefix}: {routing_summary}" if routing_summary else "", _COLOR_WHITE)
        attr_e = (getattr(d, "attribute_e", "") or "").strip().replace("\n", " | ")[:200]
        attr_e_cell = f", {_attr_cell(attr_e, _COLOR_WHITE)}" if attr_e else ""

        # Empties (omitted -> auto-completed white per RULE 16 documentation).
        rows.append(
            f"['{name}', {default_cell}, {model_cell}, {os_cell}, {stencil_cell}, {attr_d_cell}{attr_e_cell}]"
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
    dropped_ips: List[Tuple[str, str, str, str]] = field(default_factory=list)

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


def build_command_script(
    model: NSModel, port_info_unknown: Union[bool, AbstractSet[str]] = False,
) -> CommandScript:
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
    _add("2.5 port_info_bulk", cmd_rename_port_info_bulk(model, unknown=port_info_unknown))

    # Phase 3 (sub-if bindings -> portchannel -> virtual_port -> l2_segment).
    # Sub-interface bindings run first so their parent L1 ports (from Phase 2)
    # exist and the sub-ifs are ready before Phase 4 assigns their IPs.
    _add_many("3 vport_l1if_direct_binding", cmd_add_vport_l1if_direct_binding(model))
    _add_many("3 vport_l2_direct_binding", cmd_add_vport_l2_direct_binding(model))
    _add("3 portchannel_bulk", cmd_add_portchannel_bulk(model))
    _add("3 virtual_port_bulk", cmd_add_virtual_port_bulk(model))
    _add("3 l2_segment_bulk", cmd_add_l2_segment_bulk(model))

    # Phase 4 (IP -> VRF)
    dropped_ips: List[Tuple[str, str, str, str]] = []
    _add("4 ip_address_bulk", cmd_add_ip_address_bulk(model, dropped_ips))
    _add_many("4 l3_instance (VRF)", cmd_rename_l3_instance(model))

    # Phase 6 (attribute_bulk)
    _add("6 attribute_bulk", cmd_rename_attribute_bulk(model))

    return CommandScript(lines=lines, counts=counts, dropped_ips=dropped_ips)
