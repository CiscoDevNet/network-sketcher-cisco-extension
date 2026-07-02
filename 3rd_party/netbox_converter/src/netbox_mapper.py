# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""netbox_mapper.py — build an NSModel from indexed NetBox data.

Turns the ``NetboxData`` (core DCIM/IPAM) into the converter-agnostic
``NSModel`` that ``ns_command_builder`` serialises. Layer coverage:

* **L1** — device-to-device physical links from each interface's
  ``connected_endpoints`` (NetBox resolves patch-panel / rear-front pass-through
  chains to the real far-end interface, so patch panels drop out naturally).
* **L2** — access ports (``untagged_vlan``) and trunk ports (``tagged_vlans``);
  ``virtual`` SVIs (``VlanN``) get a self-bind segment (RULE 15).
* **L3** — interface IP addresses (physical port or SVI) + per-interface VRF.
* Port-channels from LAG membership; SVIs / loopbacks from ``virtual`` ifaces.

**Colour convention:** real NetBox devices are role-coloured by the shared
``ns_command_builder`` palette (green network gear / red server / yellow client),
identical to the sna / cv / cml / aci / nd converters — so they carry *no*
``default_color`` override and let the builder pick the role colour. Two special
families override the Default cell:

* **Observed WayPoints** — a WAN / provider WayPoint that is backed by a real
  NetBox record (a ``circuits.providernetwork`` such as "Level3 MPLS") gets
  NS's native **light blue** WayPoint colour ``(220,230,242)``. It is *observed*
  (it exists in the source), so it is not flagged inferred.
* **Synthesised shapes** — the ``dummy_stub_*`` placeholders that stand in for
  uncabled VLAN/IP ports are forced light **gray** ``(200,200,200)`` (the shared
  palette's "inferred / not observed reality").
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import netbox_stencil_mapper as sten
from . import netbox_layout as layout
from .netbox_reader import NetboxData
from .ns_model import (
    NSDevice, NSIPAssignment, NSL1Link, NSL2Segment, NSModel,
    NSPortChannel, NSVirtualPort, build_area_layout, normalise_port_name,
)

# Shared-palette light gray: "inferred / external / not observed reality".
# Only forced onto synthesised dummy_stub_* placeholders (see module docstring).
GRAY = (200, 200, 200)
# NS's native WayPoint colour (light blue). Used for OBSERVED WayPoints — a WAN /
# provider WayPoint backed by a real NetBox record (circuits.providernetwork).
OBSERVED_WAYPOINT = (220, 230, 242)

# RULE 0 vertical tier per stencil type.
_TIER = {
    sten.NS_CLOUD: 0,
    sten.NS_ROUTER: 1, sten.NS_FIREWALL: 1,
    sten.NS_WLC: 2,
    sten.NS_L3SWITCH: 3,
    sten.NS_SWITCH: 4, sten.NS_AP: 4,
    sten.NS_SERVER: 5, sten.NS_PC: 5, sten.NS_PHONE: 5,
}


@dataclass
class MapReport:
    devices: int = 0
    l1_links: int = 0
    ip_assignments: int = 0
    port_channels: int = 0
    svis: int = 0
    l2_access: int = 0
    l2_trunk: int = 0
    vrf_renames: int = 0
    skipped_circuit_links: int = 0
    l1_port_conflicts: int = 0
    skipped_ip_no_port: int = 0
    skipped_l2_no_port: int = 0
    skipped_svi_no_l1: int = 0
    skipped_pc_member: int = 0
    network_groups: int = 0
    unconnected_dropped: int = 0
    unconnected_kept: int = 0
    wan_waypoints: int = 0
    wan_waypoint_links: int = 0
    host_stubs: int = 0
    host_stub_ports: int = 0
    non_cisco_ports: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


def _vid_from_name(name: str) -> Optional[int]:
    m = re.search(r"(\d+)", name or "")
    return int(m.group(1)) if m else None


# NS only accepts a fixed family of Cisco interface tokens; anything else is
# rejected by the engine ("Invalid from_port"). NetBox is multi-vendor, so map
# common non-Cisco interface names to the nearest NS-accepted token first.
_VENDOR_PREFIX = [
    # Juniper speed prefixes: xe=10G, ge=1G, et/fte=40G+, xle/xlge=100G, ae=LAG.
    (re.compile(r"^xe-?(?=\d)", re.I), "TenGigabitEthernet "),
    (re.compile(r"^ge-?(?=\d)", re.I), "GigabitEthernet "),
    (re.compile(r"^(?:et|fte)-?(?=\d)", re.I), "FortyGigabitEthernet "),
    (re.compile(r"^(?:xle|xlge)-?(?=\d)", re.I), "HundredGigE "),
    (re.compile(r"^ae-?(?=\d)", re.I), "Port-channel "),
    (re.compile(r"^fxp-?(?=\d)", re.I), "Management "),
]

_NS_VALID = re.compile(
    r"^(GigabitEthernet|TenGigabitEthernet|TwentyFiveGigE|FortyGigabitEthernet|"
    r"HundredGigE|FastEthernet|Ethernet|Management|mgmt|Loopback|Vlan|"
    r"Port-channel|Serial|Tunnel|nve) ")


def _looks_cisco_port(norm_name: str) -> bool:
    """True if the name is an NS-accepted interface token."""
    return bool(_NS_VALID.match(norm_name))


def _ns_port_name(raw: str) -> str:
    """Return an NS-accepted port name for any NetBox interface name.

    1. Rewrite common non-Cisco (Juniper) speed prefixes to their Cisco token.
    2. Apply the shared Cisco normaliser.
    3. If still not NS-valid, fall back to 'Ethernet <n>' preserving the number,
       so a foreign name (e.g. 'fc0', 'eno1') still yields a usable, unique-ish
       port instead of aborting the whole bulk command.
    """
    s = (raw or "").strip()
    if not s:
        return s
    for pat, repl in _VENDOR_PREFIX:
        if pat.match(s):
            s = pat.sub(repl, s, count=1)
            break
    norm = normalise_port_name(s)
    if _looks_cisco_port(norm):
        return norm
    digits = re.findall(r"\d+(?:/\d+)*", raw)
    return f"Ethernet {digits[0]}" if digits else "Ethernet 0"


def build_model(data: NetboxData,
                config: Optional[dict] = None) -> Tuple[NSModel, List[sten.StencilMapping], MapReport]:
    config = config or {}
    color_overrides: Dict[str, list] = (config.get("color_overrides") or {})
    include_unconnected = bool(config.get("include_unconnected", False))
    draw_wan = bool(config.get("draw_wan_waypoints", True))
    synth_stubs = bool(config.get("synthesize_host_stubs", True))
    report = MapReport()

    model = NSModel()
    mappings: List[sten.StencilMapping] = []
    in_scope: Dict[int, str] = {}   # device_id -> NS device name

    # -- Phase A: devices + stencils ---------------------------------------
    for dev in data.devices:
        name = data.device_name(dev)
        role_slug, role_name = data.device_role(dev)
        plat_slug, plat_name = data.device_platform(dev)
        mapping = sten.map_device(
            name=name, role_slug=role_slug, role_name=role_name,
            model=data.device_model(dev),
            platform_slug=plat_slug, platform_name=plat_name,
            manufacturer=data.device_manufacturer(dev),
        )
        mappings.append(mapping)
        area = (data.device_site_slug(dev) or "default")
        row = _TIER.get(mapping.stencil_type, 4)
        # Real devices are role-coloured by ns_command_builder's shared palette;
        # only apply an explicit override when the config supplies one.
        override = color_overrides.get(name)
        model.devices[name] = NSDevice(
            name=name, area=area, row=row, stencil=mapping,
            is_endpoint=sten.is_endpoint_stencil(mapping.stencil_type),
            default_color=tuple(override) if override else None,
        )
        in_scope[dev["id"]] = name
    report.devices = len(model.devices)

    # -- Phase B: L1 links (device-to-device via connected_endpoints) --------
    # NS is strict: each physical port belongs to at most ONE L1 link, and
    # `add l1_link_bulk` is all-or-nothing (one bad row rejects the whole
    # command). NetBox `connected_endpoints` can resolve a single port to
    # several far ends (patch-panel fan-out / multi-cable quirks), so we keep
    # the first link that claims each (device, port) and drop later conflicts.
    seen_links: set = set()
    used_ports: set = set()   # (device, port) already consumed by a link
    for iface in data.interfaces:
        dev = iface.get("device") or {}
        if dev.get("id") not in in_scope:
            continue
        ce_type = iface.get("connected_endpoints_type")
        if ce_type != "dcim.interface":
            continue   # circuit / provider-network ends handled below
        if not iface.get("connected_endpoints_reachable"):
            continue
        a_dev = in_scope[dev["id"]]
        a_port = _ns_port_name(iface.get("name", ""))
        for ep in (iface.get("connected_endpoints") or []):
            peer_dev_obj = ep.get("device") or {}
            peer_dev_id = peer_dev_obj.get("id")
            if peer_dev_id not in in_scope:
                continue
            b_dev = in_scope[peer_dev_id]
            b_port = _ns_port_name(ep.get("name", ""))
            if a_dev == b_dev:
                continue
            key = frozenset({(a_dev, a_port), (b_dev, b_port)})
            if key in seen_links:
                continue
            a_key, b_key = (a_dev, a_port), (b_dev, b_port)
            if a_key in used_ports or b_key in used_ports:
                report.l1_port_conflicts += 1
                continue
            seen_links.add(key)
            used_ports.add(a_key)
            used_ports.add(b_key)
            model.l1_links.append(NSL1Link(a_dev, a_port, b_dev, b_port))
            for pn in (a_port, b_port):
                if not _looks_cisco_port(pn):
                    report.non_cisco_ports.append(pn)

    # WAN / circuit waypoints: an interface that terminates on a circuit or
    # provider network is drawn as a link to a shared gray cloud (one waypoint
    # per far-end name), mirroring the offline tool's single-line 'Connection'
    # peer. This both draws the WAN edge and makes that port exist, so any IP /
    # VLAN on it becomes reflectable. Disabled with draw_wan_waypoints=false.
    wp_port_idx: Dict[str, int] = {}
    for iface in data.interfaces:
        dev = iface.get("device") or {}
        if dev.get("id") not in in_scope:
            continue
        ce_type = iface.get("connected_endpoints_type")
        if not (ce_type and str(ce_type).startswith("circuits.")):
            continue
        if not iface.get("connected_endpoints_reachable"):
            continue
        if not draw_wan:
            report.skipped_circuit_links += 1
            continue
        a_dev = in_scope[dev["id"]]
        a_port = _ns_port_name(iface.get("name", ""))
        if (a_dev, a_port) in used_ports:
            continue
        eps = iface.get("connected_endpoints") or []
        ep = eps[0] if eps else {}
        wp_name = ep.get("name") or ep.get("display") or "WAN"
        if wp_name not in model.devices:
            wp_map = sten.map_waypoint(wp_name)
            # 'wan' is a NS waypoint area name (build_area_layout promotes it to
            # 'wan_wp_'), which is what makes NS render it as a WayPoint cloud
            # rather than a device. This WayPoint is OBSERVED (a real NetBox
            # circuits.providernetwork record), so it gets NS's native light-blue
            # WayPoint colour rather than the inferred/synthesised gray.
            model.devices[wp_name] = NSDevice(
                name=wp_name, area="wan", row=0, stencil=wp_map,
                is_endpoint=False, default_color=OBSERVED_WAYPOINT)
            mappings.append(wp_map)
            report.wan_waypoints += 1
        idx = wp_port_idx.get(wp_name, 0)
        wp_port = f"Ethernet {idx}"
        wp_port_idx[wp_name] = idx + 1
        used_ports.add((a_dev, a_port))
        used_ports.add((wp_name, wp_port))
        model.l1_links.append(NSL1Link(a_dev, a_port, wp_name, wp_port))
        report.wan_waypoint_links += 1
    report.l1_links = len(model.l1_links)

    # Ports/devices that will actually EXIST in NS after L1 import. Only these
    # can carry a virtual port, L2 segment, or IP (there is no standalone
    # "add l1_interface" command — ports are born from links or portchannels).
    existing_ports: set = set(used_ports)
    devices_with_l1: set = {d for (d, _p) in used_ports}

    # -- Phase B3 (④ host-stub synthesis): an uncabled port that carries a VLAN
    # or IP cannot exist in NS (no standalone-port command), so give it a
    # synthetic peer. ONE 'dummy_stub_<k>' device is created per real device,
    # and ALL of that device's uncabled VLAN/IP ports link to it, each on its
    # own 'Dummy <j>' interface (j = 0,1,...). So a switch with 8 such ports
    # links to a single dummy_stub with 8 Dummy interfaces (not 8 stubs).
    # Disabled with synthesize_host_stubs=false.
    stub_parent: Dict[str, str] = {}   # stub name -> the real device it hangs off
    if synth_stubs:
        orphan_by_dev: Dict[str, List[str]] = {}
        for iface in data.interfaces:
            dev = iface.get("device") or {}
            if dev.get("id") not in in_scope:
                continue
            if (iface.get("type") or {}).get("value") == "virtual":
                continue  # SVIs/loopbacks are virtual ports, not physical
            mode = (iface.get("mode") or {}).get("value") if iface.get("mode") else None
            want_l2 = bool(mode) and bool(iface.get("untagged_vlan") or iface.get("tagged_vlans"))
            want_ip = bool(data.iface_ips(iface))
            if not (want_l2 or want_ip):
                continue
            dev_name = in_scope[dev["id"]]
            port = _ns_port_name(iface.get("name", ""))
            if (dev_name, port) in existing_ports:
                continue
            ports = orphan_by_dev.setdefault(dev_name, [])
            if port not in ports:
                ports.append(port)
        stub_k = 0
        n_stub_ports = 0
        for dev_name, ports in orphan_by_dev.items():
            stub_k += 1
            stub_name = f"dummy_stub_{stub_k}"
            stub_map = sten.map_stub(stub_name)
            model.devices[stub_name] = NSDevice(
                name=stub_name, area="", row=7, stencil=stub_map,
                is_endpoint=True, default_color=GRAY)
            mappings.append(stub_map)
            stub_parent[stub_name] = dev_name
            devices_with_l1.add(dev_name)
            devices_with_l1.add(stub_name)
            for j, port in enumerate(ports):
                stub_port = f"Dummy {j}"
                used_ports.add((dev_name, port)); used_ports.add((stub_name, stub_port))
                existing_ports.add((dev_name, port)); existing_ports.add((stub_name, stub_port))
                model.l1_links.append(NSL1Link(dev_name, port, stub_name, stub_port))
                n_stub_ports += 1
        report.host_stubs = stub_k
        report.host_stub_ports = n_stub_ports
    report.l1_links = len(model.l1_links)

    # -- Phase B2: placement = offline network-groups + tiers ---------------
    # Faithful to the offline tool: group devices by connected component and
    # tier each by centrality + role keywords. Devices with no cabled link are
    # not in the graph, so — like the offline tool — they are not placed.
    # Give the tiering NetBox's real device role (stencil) so it does not rely
    # on name keywords / degree alone (fixes routers named without 'rtr' and
    # switches whose degree is inflated by host stubs).
    stencil_tiers = {n: _TIER.get(d.stencil.stencil_type, 4)
                     for n, d in model.devices.items()}
    components, tiers = layout.compute_network_groups_and_tiers(
        model.l1_links, stencil_tiers)
    node_group: Dict[str, str] = {}
    gwidth = max(2, len(str(len(components))))
    for idx, comp in enumerate(components, 1):
        gid = f"grp{idx:0{gwidth}d}"
        for n in comp:
            node_group[n] = gid
    connected = set(node_group)
    unconnected = [n for n in model.devices if n not in connected]
    if include_unconnected:
        # Keep uncabled devices in a dedicated 'unlinked' area (they have no
        # ports, so they carry no links/L2/IP — just shown for completeness).
        for n in unconnected:
            model.devices[n].area = "unlinked"
            model.devices[n].row = _TIER.get(model.devices[n].stencil.stencil_type, 6)
        report.unconnected_kept = len(unconnected)
    else:
        for n in unconnected:
            del model.devices[n]
        report.unconnected_dropped = len(unconnected)
    report.network_groups = len(components)
    report.devices = len(model.devices)

    # Leaf-below-parent: a single-homed real device (e.g. an access switch that
    # hangs off one distribution switch) should sit one tier BELOW its uplink,
    # not beside it — this keeps its single link short, straight and crossing-
    # free. Generic (degree + tier based, no device names). Synthetic stubs are
    # excluded from the adjacency so a device is not pushed under its own stub.
    stub_names = {n for n, d in model.devices.items()
                  if d.stencil.stencil_type == sten.NS_PC and n.startswith("dummy_stub_")}
    real_adj: Dict[str, set] = defaultdict(set)
    for lk in model.l1_links:
        if lk.a_device in stub_names or lk.b_device in stub_names:
            continue
        if lk.a_device != lk.b_device:
            real_adj[lk.a_device].add(lk.b_device)
            real_adj[lk.b_device].add(lk.a_device)
    base_tiers = dict(tiers)
    for name, nbrs in real_adj.items():
        if len(nbrs) != 1:
            continue
        parent = next(iter(nbrs))
        # Only push under a genuine uplink (a non-leaf parent). This avoids
        # inverting a mutual-leaf pair (e.g. an L3 switch <-> a single server),
        # where the more-senior node must stay on top.
        if len(real_adj.get(parent, ())) >= 2 \
                and name in base_tiers and parent in base_tiers \
                and base_tiers[name] <= base_tiers[parent]:
            tiers[name] = base_tiers[parent] + 1

    for name in connected:
        d = model.devices[name]
        # WAN clouds (detected structurally via circuits.*, not by name) keep
        # their dedicated 'wan' -> 'wan_wp_' area so NS renders them as a
        # WayPoint cloud; only real devices/stubs go into the network group.
        if d.stencil.stencil_type == sten.NS_CLOUD:
            continue
        d.area = node_group[name]
        d.row = tiers.get(name, 6)

    # Place each synthetic stub on the SAME row as its parent's nearest existing
    # lower tier (e.g. a router's stub joins the switch row directly beneath it),
    # rather than creating a new phantom intermediate row. Combined with the
    # crossing-min column ordering (which barycentres the stub under its single
    # neighbour = the parent), this puts the stub right beneath its parent with
    # the shortest, least-crossing link. Falls back to parent+1 if the parent is
    # already the bottom row of its area.
    area_occupied_tiers: Dict[str, set] = defaultdict(set)
    for name, d in model.devices.items():
        if name in stub_parent:            # ignore stubs themselves
            continue
        if d.stencil.stencil_type == sten.NS_CLOUD:  # ignore WAN clouds (own area)
            continue
        area_occupied_tiers[d.area].add(d.row)
    for stub_name, parent in stub_parent.items():
        if stub_name not in model.devices or parent not in model.devices:
            continue
        pdev = model.devices[parent]
        model.devices[stub_name].area = pdev.area
        below = [t for t in area_occupied_tiers[pdev.area] if t > pdev.row]
        model.devices[stub_name].row = min(below) if below else pdev.row + 1

    # -- Phase C: port-channels (LAG membership) ----------------------------
    lag_groups: Dict[Tuple[str, str], List[str]] = defaultdict(list)
    for iface in data.interfaces:
        dev = iface.get("device") or {}
        if dev.get("id") not in in_scope:
            continue
        lag = iface.get("lag")
        if not lag:
            continue
        pc_name = _ns_port_name(lag.get("name", ""))
        member = _ns_port_name(iface.get("name", ""))
        lag_groups[(in_scope[dev["id"]], pc_name)].append(member)
    pc_member_ports: set = set()   # (device, port) bundled into a portchannel
    for (dev_name, pc_name), members in lag_groups.items():
        # Only bundle members that exist as L1 ports; NS needs real ports.
        real_members = sorted({m for m in members if (dev_name, m) in existing_ports})
        if not real_members:
            continue
        model.port_channels.append(
            NSPortChannel(device=dev_name, physical_ports=real_members,
                          portchannel_name=pc_name))
        existing_ports.add((dev_name, pc_name))  # PC is now an assignable port
        for m in real_members:
            pc_member_ports.add((dev_name, m))
    report.port_channels = len(model.port_channels)

    # -- Phase D: SVIs / loopbacks (virtual interfaces) ---------------------
    svi_ports: Dict[Tuple[str, str], int] = {}   # (device, port) -> vid, for IP step
    for iface in data.interfaces:
        dev = iface.get("device") or {}
        if dev.get("id") not in in_scope:
            continue
        if (iface.get("type") or {}).get("value") != "virtual":
            continue
        dev_name = in_scope[dev["id"]]
        # NS attaches a virtual port to a device that already has an L1 port;
        # a device with no cabled link cannot host an SVI / loopback here.
        if dev_name not in devices_with_l1:
            report.skipped_svi_no_l1 += 1
            continue
        raw = iface.get("name", "")
        low = raw.lower()
        if low.startswith("vlan") or low.startswith("vl"):
            vid = _vid_from_name(raw)
            if vid is None:
                continue
            port = f"Vlan {vid}"
            model.virtual_ports.append(NSVirtualPort(device=dev_name, port=port, vlan_id=vid))
            model.l2_segments_svi.append(NSL2Segment(device=dev_name, port=port, vlans=[f"Vlan{vid}"]))
            svi_ports[(dev_name, port)] = vid
            existing_ports.add((dev_name, port))
            report.svis += 1
        elif low.startswith("loop") or low.startswith("lo"):
            port = _ns_port_name(raw)
            model.virtual_ports.append(NSVirtualPort(device=dev_name, port=port, is_loopback=True))
            svi_ports[(dev_name, port)] = -1
            existing_ports.add((dev_name, port))

    # -- Phase E: L2 on physical ports (access / trunk) ---------------------
    for iface in data.interfaces:
        dev = iface.get("device") or {}
        if dev.get("id") not in in_scope:
            continue
        mode = (iface.get("mode") or {}).get("value") if iface.get("mode") else None
        if not mode:
            continue
        dev_name = in_scope[dev["id"]]
        port = _ns_port_name(iface.get("name", ""))
        if (dev_name, port) in pc_member_ports:
            report.skipped_pc_member += 1   # bundled into a portchannel -> L2/L3 goes on the PC, not the member
            continue
        if (dev_name, port) not in existing_ports:
            report.skipped_l2_no_port += 1   # access/trunk port has no cable -> no NS port
            continue
        vids: List[int] = []
        unt = iface.get("untagged_vlan")
        if unt and unt.get("vid") is not None:
            vids.append(unt["vid"])
        for tv in (iface.get("tagged_vlans") or []):
            if tv.get("vid") is not None:
                vids.append(tv["vid"])
        if not vids:
            continue
        model.l2_segments_phys.append(
            NSL2Segment(device=dev_name, port=port, vlans=[f"Vlan{v}" for v in vids]))
        if mode == "access":
            report.l2_access += 1
        else:
            report.l2_trunk += 1

    # -- Phase F: IP addresses + VRF ---------------------------------------
    for iface in data.interfaces:
        dev = iface.get("device") or {}
        if dev.get("id") not in in_scope:
            continue
        cidrs = data.iface_ips(iface)
        if not cidrs:
            continue
        dev_name = in_scope[dev["id"]]
        raw = iface.get("name", "")
        is_virtual = (iface.get("type") or {}).get("value") == "virtual"
        if is_virtual:
            vid = _vid_from_name(raw)
            port = f"Vlan {vid}" if (raw.lower().startswith(("vlan", "vl")) and vid) \
                else _ns_port_name(raw)
        else:
            port = _ns_port_name(raw)
        if (dev_name, port) in pc_member_ports:
            report.skipped_pc_member += 1   # IP on a portchannel member -> belongs on the PC, not the member
            continue
        if (dev_name, port) not in existing_ports:
            report.skipped_ip_no_port += 1   # IP on an uncabled/absent port -> can't attach in NS
            continue
        model.ip_assignments.append(NSIPAssignment(device=dev_name, port=port, cidrs=cidrs))
        vrf = data.iface_vrf(iface)
        if vrf:
            model.vrf_renames.append((dev_name, port, vrf))
    report.ip_assignments = len(model.ip_assignments)
    report.vrf_renames = len(model.vrf_renames)

    # -- Phase G: area layout ----------------------------------------------
    # Areas are the network groups (connected components), so no L1 link ever
    # crosses an area (RULE 3 holds by construction). build_area_layout buckets
    # each area's devices by row (= offline tier) and orders columns to
    # minimise link crossings.
    model.areas, model.area_to_devices = build_area_layout(model.devices, model.l1_links)

    if report.wan_waypoint_links:
        report.notes.append(
            f"{report.wan_waypoint_links} WAN/circuit link(s) drawn to "
            f"{report.wan_waypoints} gray waypoint cloud(s).")
    if report.host_stubs:
        report.notes.append(
            f"{report.host_stub_ports} uncabled VLAN/IP port(s) attached to "
            f"{report.host_stubs} synthetic 'dummy_stub_N' peer(s) (one per "
            f"device, each port on its own 'Dummy j' interface) so the port "
            f"(and its VLAN/IP) exists in NS.")
    if report.unconnected_dropped:
        report.notes.append(
            f"{report.unconnected_dropped} device(s) omitted: no cabled L1 link "
            f"(offline-faithful — only connected devices are placed; set "
            f"include_unconnected=true to keep them).")
    if report.unconnected_kept:
        report.notes.append(
            f"{report.unconnected_kept} uncabled device(s) kept in the 'unlinked' "
            f"area (include_unconnected=true).")
    if report.network_groups:
        report.notes.append(
            f"{report.network_groups} network group(s) (connected components) "
            f"laid out; tiers from centrality + role keywords (offline logic).")
    if report.l1_port_conflicts:
        report.notes.append(
            f"{report.l1_port_conflicts} L1 link(s) dropped: the port was already "
            f"used by another link (NetBox multi-endpoint / patch-panel fan-out; "
            f"NS allows one link per port).")
    if report.skipped_svi_no_l1:
        report.notes.append(
            f"{report.skipped_svi_no_l1} SVI/loopback(s) skipped: their device has "
            f"no cabled L1 port, so NS cannot host a virtual port on it.")
    if report.skipped_pc_member:
        report.notes.append(
            f"{report.skipped_pc_member} L2/IP assignment(s) skipped: the port is "
            f"a portchannel member — in NS the L2/L3 belongs on the Port-channel, "
            f"not the bundled member.")
    if report.skipped_l2_no_port:
        report.notes.append(
            f"{report.skipped_l2_no_port} L2 VLAN assignment(s) skipped: the "
            f"access/trunk port is not cabled, so it does not exist in NS.")
    if report.skipped_ip_no_port:
        report.notes.append(
            f"{report.skipped_ip_no_port} IP address(es) skipped: assigned to an "
            f"uncabled/absent port (no NS command creates a standalone L1 port).")
    if report.non_cisco_ports:
        uniq = sorted(set(report.non_cisco_ports))
        report.notes.append(
            f"{len(uniq)} non-Cisco port name(s) may be rejected by NS "
            f"(e.g. {', '.join(uniq[:5])})")
    if report.skipped_circuit_links:
        report.notes.append(
            f"{report.skipped_circuit_links} interface(s) connect to a circuit / "
            f"provider network (WAN) — not drawn as device links in this version.")

    return model, mappings, report
