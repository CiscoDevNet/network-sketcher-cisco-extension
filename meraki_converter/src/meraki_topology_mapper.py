# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""meraki_topology_mapper.py — Meraki export -> NSModel.

Builds the converter-agnostic ``NSModel`` (devices, synthesized L1 links, L2
segments, L3 IPs) from a parsed :class:`MerakiExport`.

Why L1 is SYNTHESIZED, not discovered:
    Meraki exposes a discovered link map at ``GET /networks/{id}/topology/
    linkLayer``, but it relies on live CDP/LLDP. On the DevNet Sandbox (and any
    org whose devices are offline/virtual) that endpoint returns
    ``{"nodes":[],"links":[]}``. So this mapper builds a STANDARD Meraki branch
    topology from the device inventory:
        Internet(cloud) -- MX(gateway) -- MS(switch) -- {MR(AP), MV(camera), ...}
    These links are inferred, not observed; that caveat is recorded in the
    report. Per-port VLAN data IS real (read from ``GET /devices/{serial}/
    switch/ports``) and the MX WAN IP IS real (from the management interface).

The result serialises through the shared ``ns_command_builder`` unchanged.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from . import meraki_stencil_mapper as sm
from .meraki_reader import MerakiDevice, MerakiExport, MerakiNetwork
from .ns_model import (
    NSDevice, NSIPAssignment, NSL1Link, NSL2Segment, NSModel, NSPortChannel,
    NSVirtualPort, build_area_layout, normalise_port_name,
)

# RULE 0 tier rows per NS stencil type.
_ROW_BY_STENCIL = {
    sm.NS_CLOUD: 0,
    sm.NS_FIREWALL: 1,
    sm.NS_ROUTER: 1,
    sm.NS_L3SWITCH: 3,
    sm.NS_SWITCH: 3,
    sm.NS_WLC: 3,
    sm.NS_AP: 4,
    sm.NS_SERVER: 5,
    sm.NS_PHONE: 5,
    sm.NS_PC: 5,
}

# Canonical synthetic port names (NS requires Cisco interface tokens).
_MX_WAN_PORT = "GigabitEthernet 0/0"          # WAN1 / Internet-facing
_AP_UPLINK_PORT = "GigabitEthernet 0/0"        # MR / MV / sensor single uplink
_INTERNET_PORT = "GigabitEthernet 0/0"

# Synthetic management SVI for an MR access point. An MR is an L2 bridge with a
# SINGLE management IP on the native (untagged) VLAN; it has no real SVI, so a
# clearly-named 'Dummy_*' virtual port carries that IP (NS accepts arbitrary
# virtual-port / L2-segment names — only L1 physical ports must be Cisco tokens).
_AP_MGMT_SVI = "Dummy_mgmt 0"

# Interface name on the dummy client PC that attaches to an MR's wireless IF —
# a 'Wlan 0' port representing the PC's wireless NIC.
_AP_CLIENT_IF = "Wlan 0"


def _mask_to_prefix(mask: str) -> Optional[int]:
    """Dotted-decimal netmask -> prefix length (e.g. 255.255.255.0 -> 24)."""
    try:
        octets = [int(o) for o in mask.split(".")]
    except (ValueError, AttributeError):
        return None
    if len(octets) != 4 or any(o < 0 or o > 255 for o in octets):
        return None
    bits = "".join(f"{o:08b}" for o in octets)
    return bits.count("1")


class _NameAllocator:
    """Hand out globally-unique NS device names."""

    def __init__(self) -> None:
        self._seen: Dict[str, int] = {}

    def take(self, base: str) -> str:
        if base not in self._seen:
            self._seen[base] = 1
            return base
        self._seen[base] += 1
        return f"{base}-{self._seen[base]}"


def _stencil_for(dev: MerakiDevice, cfg: Dict[str, Any]) -> sm.StencilMapping:
    mapping = sm.map_meraki_device(
        name=dev.display_name, product_type=dev.product_type,
        model=dev.model, serial=dev.serial, os_version=dev.os_version,
    )
    override = (cfg.get("stencil_overrides") or {}).get(dev.display_name)
    if override:
        mapping.stencil_type = override
        mapping.reason += f"; stencil overridden -> {override}"
    return mapping


def build_model(
    export: MerakiExport,
    cfg: Optional[Dict[str, Any]] = None,
    layout: str = "tier",
) -> Tuple[NSModel, Dict[str, Any]]:
    cfg = cfg or {}
    synth_l1 = cfg.get("synthesize_l1", True)
    add_internet = cfg.get("add_internet_waypoint", True)
    include_clients = cfg.get("include_clients", False)
    color_overrides = cfg.get("color_overrides") or {}
    ap_mgmt_model = cfg.get("ap_management_model", True)
    untag_name = cfg.get("ap_untagged_segment_name") or "untag"

    inc = set(cfg.get("network_include") or [])
    exc = set(cfg.get("network_exclude") or [])

    model = NSModel()
    mappings: List[sm.StencilMapping] = []
    caveats: List[str] = []
    counts: Dict[str, int] = {
        "networks": 0, "devices": 0, "l1_links": 0, "l2_segments": 0,
        "ip_assignments": 0, "clients": 0, "internet_waypoints": 0,
    }
    alloc = _NameAllocator()
    # serial -> NS device name, for link wiring.
    name_of: Dict[str, str] = {}
    internet_added = False
    linklayer_empty_nets: List[str] = []
    used_real_l1: List[str] = []      # networks wired from real LLDP/CDP

    def add_device(dev: MerakiDevice, area: str) -> str:
        mapping = _stencil_for(dev, cfg)
        nsname = alloc.take(dev.display_name)
        row = _ROW_BY_STENCIL.get(mapping.stencil_type, 5)
        color = color_overrides.get(nsname)
        attr = f"Meraki {dev.product_type} {dev.model} serial={dev.serial}".strip()
        model.devices[nsname] = NSDevice(
            name=nsname, area=area, row=row, stencil=mapping,
            is_endpoint=mapping.stencil_type in (sm.NS_PC, sm.NS_PHONE),
            routing_attribute=attr,
            default_color=tuple(color) if color else None,
        )
        mappings.append(mapping)
        name_of[dev.serial] = nsname
        return nsname

    for net in export.networks:
        if inc and net.id not in inc and net.name not in inc:
            continue
        if net.id in exc or net.name in exc:
            continue
        counts["networks"] += 1
        area = net.name or net.id
        devs = export.devices_in(net.id)
        if not devs:
            continue

        # Classify devices in this network.
        gateways: List[MerakiDevice] = []
        switches: List[MerakiDevice] = []
        leaves: List[MerakiDevice] = []      # APs / cameras / sensors / other
        for dev in devs:
            add_device(dev, area)
            pt = dev.product_type
            if pt == "appliance":
                gateways.append(dev)
            elif pt == "switch":
                switches.append(dev)
            else:
                leaves.append(dev)
            counts["devices"] += 1

        detail = export.detail(net.id)
        ll = detail.get("topologyLinkLayer") or {}
        if isinstance(ll, dict) and not (ll.get("links") or []):
            linklayer_empty_nets.append(area)

        if not synth_l1:
            continue

        # ---- Synthesize the standard branch topology for this network ----
        core_switch = switches[0] if switches else None
        ap_native_seg: Dict[str, str] = {}     # AP serial -> native L2 segment name
        sw_port_idx: Dict[str, int] = {}       # serial -> next access-port cursor
        vlan_universe = _network_vlan_universe(export, devs, detail)
        # Live port status (item 2): prefer real Connected access ports + the
        # flagged uplink port; falls back to config order when status is absent.
        port_plan = {sw.serial: _switch_port_plan(export, sw.serial) for sw in switches}
        acc_plan_idx: Dict[str, int] = {}

        def _port_cfg(sw: MerakiDevice, pid: str) -> Optional[Dict[str, Any]]:
            for p in (export.switch_ports.get(sw.serial) or []):
                if str(p.get("portId")) == str(pid):
                    return p
            return None

        def switch_access_port(sw: MerakiDevice) -> Tuple[str, List[str], int]:
            """Next free access port on a switch + its real VLAN list and native
            VLAN id (if known). Uses live-connected ports first, else config
            order. The native id lets an attached MR access point reuse the
            switch's native L2 segment name (so they share a broadcast domain)."""
            conn = port_plan.get(sw.serial, ([], None))[0]
            j = acc_plan_idx.get(sw.serial, 0)
            if j < len(conn):                      # status-driven: real connected port
                acc_plan_idx[sw.serial] = j + 1
                pid = conn[j]
                cfg = _port_cfg(sw, pid)
                vlans = _port_vlans(cfg, vlan_universe) if cfg else ["Vlan1"]
                return normalise_port_name(f"GigabitEthernet 1/0/{pid}"), vlans, _port_native(cfg)
            ports = export.switch_ports.get(sw.serial) or []
            idx = sw_port_idx.get(sw.serial, 0)
            sw_port_idx[sw.serial] = idx + 1
            if idx < len(ports):
                p = ports[idx]
                pid = str(p.get("portId", idx + 1))
                vlans = _port_vlans(p, vlan_universe)
                native = _port_native(p)
            else:
                pid = str(idx + 1)
                vlans = ["Vlan1"]
                native = 1
            return normalise_port_name(f"GigabitEthernet 1/0/{pid}"), vlans, native

        def switch_uplink_port(sw: MerakiDevice) -> Tuple[str, List[str]]:
            """The switch's uplink port: the status-flagged uplink if known,
            else the highest trunk port."""
            uplink_pid = port_plan.get(sw.serial, ([], None))[1]
            if uplink_pid is not None:
                cfg = _port_cfg(sw, uplink_pid)
                vlans = _port_vlans(cfg, vlan_universe) if cfg else ["Vlan1"]
                return normalise_port_name(f"GigabitEthernet 1/0/{uplink_pid}"), vlans
            ports = export.switch_ports.get(sw.serial) or []
            uplinks = [p for p in ports if str(p.get("type")) == "trunk"]
            chosen = uplinks[-1] if uplinks else (ports[-1] if ports else None)
            if chosen is not None:
                pid = str(chosen.get("portId", 25))
                return normalise_port_name(f"GigabitEthernet 1/0/{pid}"), _port_vlans(chosen, vlan_universe)
            return normalise_port_name("GigabitEthernet 1/0/25"), ["Vlan1"]

        def link(a_name: str, a_port: str, b_name: str, b_port: str,
                 vlans: Optional[List[str]] = None,
                 vlans_b: Optional[List[str]] = None) -> None:
            model.l1_links.append(NSL1Link(a_name, a_port, b_name, b_port))
            counts["l1_links"] += 1
            if vlans:
                model.l2_segments_phys.append(NSL2Segment(a_name, a_port, vlans))
                counts["l2_segments"] += 1
            if vlans_b:
                model.l2_segments_phys.append(NSL2Segment(b_name, b_port, vlans_b))
                counts["l2_segments"] += 1

        # 1) Internet cloud <-> each gateway WAN, plus WAN + LAN IPs (L3).
        for gi, gw in enumerate(gateways):
            gw_name = name_of[gw.serial]
            gw_lan_port = normalise_port_name(f"GigabitEthernet 0/{gi + 2}")
            mi = export.management_interfaces.get(gw.serial, {})
            wan1 = (mi.get("wan1") or {}) if isinstance(mi, dict) else {}
            mask = wan1.get("staticSubnetMask")
            wan_pfx = _mask_to_prefix(mask) if mask else None
            if add_internet:
                if not internet_added:
                    iname = alloc.take("Internet")
                    model.devices[iname] = NSDevice(
                        name=iname, area="internet", row=0,
                        stencil=sm.map_internet_cloud(iname), is_endpoint=False,
                        routing_attribute="Synthetic WAN/Internet waypoint",
                    )
                    mappings.append(model.devices[iname].stencil)
                    _internet_name = iname
                    internet_added = True
                    counts["internet_waypoints"] += 1
                else:
                    _internet_name = next(
                        n for n, d in model.devices.items() if d.area == "internet")
                model.l1_links.append(NSL1Link(_internet_name, _INTERNET_PORT, gw_name, _MX_WAN_PORT))
                counts["l1_links"] += 1
                # Internet-side IP = the MX's WAN default gateway (the upstream
                # next hop), in the same subnet as the WAN IP. Only the first
                # gateway labels the shared cloud port.
                gwip = wan1.get("staticGatewayIp")
                if gwip and gi == 0:
                    cidr = f"{gwip}/{wan_pfx}" if wan_pfx is not None else f"{gwip}/24"
                    model.ip_assignments.append(
                        NSIPAssignment(_internet_name, _INTERNET_PORT, [cidr]))
                    counts["ip_assignments"] += 1
            # WAN static IP -> L3 on the gateway WAN port.
            ip, mask = wan1.get("staticIp"), wan1.get("staticSubnetMask")
            if ip and mask:
                pfx = _mask_to_prefix(mask)
                cidr = f"{ip}/{pfx}" if pfx is not None else ip
                model.ip_assignments.append(NSIPAssignment(gw_name, _MX_WAN_PORT, [cidr]))
                counts["ip_assignments"] += 1
            elif gw.wan1_ip:
                model.ip_assignments.append(NSIPAssignment(gw_name, _MX_WAN_PORT, [gw.wan1_ip]))
                counts["ip_assignments"] += 1
            # LAN-side L3 — the default gateway for the LAN, without which there
            # is no route from LAN hosts to the Internet. Single-LAN networks
            # carry it in applianceSingleLan; VLAN-enabled networks expose one
            # L3 interface per appliance VLAN.
            _add_appliance_l3(model, gw_name, gw_lan_port, detail, counts)

        # Inter-device L1: use REAL LLDP/CDP neighbours when present; otherwise
        # synthesize the standard MX -> MS -> leaves branch (steps 2-4).
        parent = core_switch or (gateways[0] if gateways else None)
        real_links = _real_l1_links(export, devs, name_of)
        if real_links:
            used_real_l1.append(area)
            sp_index = {
                d.serial: {str(p.get("portId")): p
                           for p in (export.switch_ports.get(d.serial) or [])}
                for d in switches
            }
            for a_ns, a_port, a_sw, b_ns, b_port, b_sw in real_links:
                model.l1_links.append(NSL1Link(a_ns, a_port, b_ns, b_port))
                counts["l1_links"] += 1
                for ns, port, sw in ((a_ns, a_port, a_sw), (b_ns, b_port, b_sw)):
                    if sw is not None:
                        p = sp_index.get(sw[0], {}).get(sw[1])
                        if p is not None:
                            model.l2_segments_phys.append(
                                NSL2Segment(ns, port, _port_vlans(p, vlan_universe)))
                            counts["l2_segments"] += 1
        else:
            # 2) Gateway(s) <-> core switch (MX LAN port -> switch uplink).
            # The MX LAN port's L2/L3 nature is owned by _add_appliance_l3 (a
            # routed L3 interface in single-LAN mode, or an L2 trunk when VLANs
            # are enabled), so NO L2 segment is bound on the gateway side here —
            # binding both L2 and an IP on one port makes NS reject the IP.
            if core_switch is not None and gateways:
                up_port, up_vlans = switch_uplink_port(core_switch)
                for i, gw in enumerate(gateways):
                    gw_lan = normalise_port_name(f"GigabitEthernet 0/{i + 2}")
                    link(name_of[gw.serial], gw_lan, name_of[core_switch.serial], up_port,
                         vlans=None, vlans_b=up_vlans)

            # 3) Extra switches <-> core switch.
            for sw in switches[1:]:
                up_port, up_vlans = switch_uplink_port(sw)
                acc_port, acc_vlans, _ = switch_access_port(core_switch)
                link(name_of[sw.serial], up_port, name_of[core_switch.serial], acc_port,
                     vlans=up_vlans, vlans_b=acc_vlans)

            # 4) Leaves (AP / camera / sensor) <-> core switch (or gateway).
            for leaf in leaves:
                if parent is None:
                    break
                if parent is core_switch:
                    acc_port, acc_vlans, acc_native = switch_access_port(parent)
                    link(name_of[parent.serial], acc_port, name_of[leaf.serial], _AP_UPLINK_PORT,
                         vlans=acc_vlans)
                    # An MR reuses the switch's native L2 segment name so its
                    # untagged domain shares the switch broadcast domain.
                    if leaf.product_type == "wireless":
                        ap_native_seg[leaf.serial] = f"Vlan{acc_native}"
                else:  # attach to gateway LAN (gateway port is L3, owned above)
                    gw_lan = normalise_port_name(f"GigabitEthernet 0/{2 + leaves.index(leaf)}")
                    link(name_of[parent.serial], gw_lan, name_of[leaf.serial], _AP_UPLINK_PORT,
                         vlans=None)

        # 5) Optional clients as endpoints hanging off the core switch / gateway.
        if include_clients and parent is not None:
            client_seq = 0
            for cl in (detail.get("clients") or []):
                # Observed clients use the shared 'PC_{site}_{n}_{seq}' name
                # (n=1: one real IP per client). The original description/MAC is
                # kept in the routing attribute for traceability.
                client_seq += 1
                desc = cl.get("description") or cl.get("mac") or "client"
                cname = alloc.take(f"PC_{_site_code(area)}_1_{client_seq}")
                model.devices[cname] = NSDevice(
                    name=cname, area=area, row=5,
                    stencil=sm.map_client(cname, str(cl.get("manufacturer", ""))),
                    is_endpoint=True,
                    routing_attribute=f"client {desc} mac={cl.get('mac', '')}",
                )
                mappings.append(model.devices[cname].stencil)
                ip = cl.get("ip")
                if parent is core_switch:
                    acc_port, acc_vlans, _ = switch_access_port(parent)
                    model.l1_links.append(NSL1Link(name_of[parent.serial], acc_port, cname, _AP_UPLINK_PORT))
                else:
                    gw_lan = normalise_port_name("GigabitEthernet 0/9")
                    model.l1_links.append(NSL1Link(name_of[parent.serial], gw_lan, cname, _AP_UPLINK_PORT))
                counts["l1_links"] += 1
                if ip:
                    model.ip_assignments.append(NSIPAssignment(cname, _AP_UPLINK_PORT, [f"{ip}/24"]))
                    counts["ip_assignments"] += 1
                counts["clients"] += 1

        # 6) L3 switch SVIs (switch/routing/interfaces) + 7) link-aggregation
        #    port-channels.
        for sw in switches:
            ifaces = export.switch_routing.get(sw.serial) or []
            if ifaces:
                _add_switch_l3(model, name_of[sw.serial], ifaces, counts)
        _add_port_channels(model, detail, name_of, counts)

        # 8) Leaf (AP / camera / sensor) L2 + management IP.
        #    An MR access point is an L2 bridge, modelled as:
        #      - the wired uplink (GE0/0) bound to the switch's NATIVE L2 segment
        #        name (so the untagged domain shares the switch broadcast domain),
        #        plus any VLAN-tagged SSID VLANs;
        #      - a synthetic 'Dummy_mgmt 0' management SVI on that native segment,
        #        carrying the lanIp when one exists;
        #      - each enabled SSID as a PHYSICAL wireless IF ('<SSID name> <n>',
        #        or 'wlan <n>' when the name is unknown) connected DOWN to a
        #        synthetic dummy PC (NOT to the switch) standing in for that
        #        SSID's wireless clients.
        #    Cameras/sensors keep the simpler model: lanIp on the physical uplink.
        #    lanIp is null on dormant/virtual devices (sandbox), set on live orgs.
        single = detail.get("applianceSingleLan") or {}
        lan_pfx = _subnet_prefix(single.get("subnet", "")) if single.get("subnet") else "24"
        site = _site_code(area)
        dummy_pc_seq = 0
        port_of: Dict[str, str] = {}
        for lk in model.l1_links:
            port_of.setdefault(lk.a_device, lk.a_port)
            port_of.setdefault(lk.b_device, lk.b_port)
        for leaf in leaves:
            ns = name_of.get(leaf.serial)
            port = port_of.get(ns)
            if not ns:
                continue
            is_ap = leaf.product_type == "wireless"
            if is_ap and ap_mgmt_model and port:
                native_seg = ap_native_seg.get(leaf.serial, untag_name)
                ssid_ports = _ap_ssid_ports(detail, native_seg)
                tagged = sorted({seg for _, seg in ssid_ports if seg != native_seg})
                # Wired uplink: native segment (+ tagged SSID VLANs = trunk).
                model.l2_segments_phys.append(
                    NSL2Segment(ns, port, [native_seg, *tagged]))
                counts["l2_segments"] += 1
                # Management SVI on the native segment, carrying lanIp if present.
                model.virtual_ports.append(NSVirtualPort(ns, _AP_MGMT_SVI))
                model.l2_segments_svi.append(NSL2Segment(ns, _AP_MGMT_SVI, [native_seg]))
                counts["l2_segments"] += 1
                if leaf.lan_ip:
                    model.ip_assignments.append(
                        NSIPAssignment(ns, _AP_MGMT_SVI, [f"{leaf.lan_ip}/{lan_pfx}"]))
                    counts["ip_assignments"] += 1
                # Each SSID -> a physical wireless IF connected DOWN to a dummy
                # PC (a synthetic stand-in for that SSID's wireless clients). The
                # IF is deliberately NOT linked to the switch. The dummy PC uses
                # the shared 'PC_{site}_{n}_{seq}' name with a client count of 0
                # and renders gray (inferred device). PC / server endpoints are
                # modelled as L3 interfaces only — the AP's wireless IF carries
                # the L2 segment, but the PC side gets NO L2 segment.
                for sport, seg in ssid_ports:
                    dummy_pc_seq += 1
                    pc_name = alloc.take(f"PC_{site}_0_{dummy_pc_seq}")
                    model.devices[pc_name] = NSDevice(
                        name=pc_name, area=area, row=5,
                        stencil=sm.map_client(pc_name, ""), is_endpoint=True,
                        routing_attribute=f"Dummy wireless client (SSID {sport} on {ns})",
                        default_color=(200, 200, 200),
                    )
                    mappings.append(model.devices[pc_name].stencil)
                    link(ns, sport, pc_name, _AP_CLIENT_IF, vlans=[seg])
            elif leaf.lan_ip and port:
                model.ip_assignments.append(
                    NSIPAssignment(ns, port, [f"{leaf.lan_ip}/{lan_pfx}"]))
                counts["ip_assignments"] += 1

    # MX HA roles + live WAN uplink status, folded into device attributes.
    caveats.extend(_annotate_ha_and_uplinks(export, name_of, model))
    # AutoVPN site-to-site overlay (spoke -> hub) + BGP AS annotation.
    caveats.extend(_add_vpn_links(export, name_of, model, counts))

    # Area layout (tier-based; Meraki carries no canvas coordinates).
    model.areas, model.area_to_devices = build_area_layout(
        model.devices, model.l1_links, layout=layout)

    # Caveats for the report.
    if used_real_l1:
        caveats.append(
            "L1 links are DISCOVERED from real LLDP/CDP neighbours for: "
            f"{', '.join(sorted(set(used_real_l1)))}.")
    synth_nets = [n for n in linklayer_empty_nets if n not in set(used_real_l1)]
    if synth_nets:
        caveats.append(
            "L1 links are SYNTHESIZED (standard MX->MS->MR/MV branch topology) for: "
            f"{', '.join(synth_nets)} — no LLDP/CDP neighbours (devices "
            "offline/virtual). Actual cabling may differ.")
    caveats.append(
        "Switch-port VLANs are read from the live config; appliance VLAN/L3 data "
        "is included only when VLANs are enabled on the network.")

    info = {"counts": counts, "caveats": caveats, "mappings": mappings}
    return model, info


def _subnet_prefix(subnet: str, default: str = "24") -> str:
    """Prefix length from a 'a.b.c.d/NN' subnet string (fallback to default)."""
    if isinstance(subnet, str) and "/" in subnet:
        return subnet.rsplit("/", 1)[-1]
    return default


def _add_appliance_l3(model: NSModel, gw_name: str, lan_port: str,
                      detail: Dict[str, Any], counts: Dict[str, int]) -> bool:
    """Add the MX LAN-side L3 interface(s) so LAN hosts have a default gateway.

    - VLANs enabled  -> one SVI per appliance VLAN on the MX (with its
      applianceIp), self-bound (RULE 15) and trunked on the LAN port.
    - VLANs disabled -> the single-LAN applianceIp goes directly on the LAN
      physical port (the MX's routed LAN interface).
    Returns True when at least one L3 interface was added.
    """
    vlans = detail.get("applianceVlans")
    if isinstance(vlans, list) and vlans:
        trunk_vlans: List[str] = []
        for v in vlans:
            vid = v.get("id")
            if vid is None:
                continue
            svi = normalise_port_name(f"Vlan {vid}")
            model.virtual_ports.append(NSVirtualPort(gw_name, svi, vlan_id=int(vid)))
            model.l2_segments_svi.append(NSL2Segment(gw_name, svi, [f"Vlan{vid}"]))
            ip, subnet = v.get("applianceIp"), v.get("subnet")
            if ip and subnet:
                model.ip_assignments.append(
                    NSIPAssignment(gw_name, svi, [f"{ip}/{_subnet_prefix(subnet)}"]))
                counts["ip_assignments"] += 1
            trunk_vlans.append(f"Vlan{vid}")
        if trunk_vlans:
            model.l2_segments_phys.append(NSL2Segment(gw_name, lan_port, trunk_vlans))
            counts["l2_segments"] += 1
        return bool(trunk_vlans)

    single = detail.get("applianceSingleLan")
    if isinstance(single, dict) and single.get("applianceIp"):
        # Single-LAN (VLANs disabled): the MX LAN gateway is modelled as an SVI
        # on the LAN VLAN (default Vlan1, matching the switch access VLAN), so
        # the L3 subnet associates with the Vlan1 L2 domain in the L3 diagram.
        # The physical LAN port stays L2 (Vlan1) toward the switch.
        vid = 1
        svi = normalise_port_name(f"Vlan {vid}")
        ip = single["applianceIp"]
        cidr = f"{ip}/{_subnet_prefix(single.get('subnet', ''))}"
        model.virtual_ports.append(NSVirtualPort(gw_name, svi, vlan_id=vid))
        model.l2_segments_svi.append(NSL2Segment(gw_name, svi, [f"Vlan{vid}"]))
        model.ip_assignments.append(NSIPAssignment(gw_name, svi, [cidr]))
        counts["ip_assignments"] += 1
        model.l2_segments_phys.append(NSL2Segment(gw_name, lan_port, [f"Vlan{vid}"]))
        counts["l2_segments"] += 1
        return True
    return False


def _site_code(name: str) -> str:
    """SNA-style short site code: the first 4 alphanumeric characters of the
    network name, capitalised (e.g. 'branch_office' -> 'Bran'). Used to build
    shared 'PC_{site}_..' / 'SRV_{site}_..' endpoint names."""
    s = re.sub(r"[^0-9A-Za-z]", "", name or "")
    return s[:4].capitalize() if s else "Site"


def _sanitise_port_token(name: str) -> str:
    """Make a free-text name safe to embed in an NS single-quoted port token.

    NS accepts arbitrary virtual-port / L2-segment names, but the command
    builder wraps them in single quotes, so quotes and commas would break the
    payload. Collapse those (and surrounding whitespace) away.
    """
    s = (name or "").strip().replace("'", "").replace('"', "").replace(",", " ")
    return " ".join(s.split())


def _ap_ssid_ports(detail: Dict[str, Any],
                   native_seg: str) -> List[Tuple[str, str]]:
    """Physical wireless ports for an AP's enabled SSIDs: (port_name, l2_segment).

    Each enabled SSID becomes a physical port. The name is the SSID name plus a
    per-SSID index ('branch_office WiFi 0'); when the SSID name is unknown it
    falls back to 'wlan <n>'. A VLAN-tagged SSID maps to its 'Vlan<id>' segment;
    an untagged SSID maps to the AP's native segment. Disabled SSIDs are skipped.
    """
    out: List[Tuple[str, str]] = []
    seen: set = set()
    idx = 0
    for s in (detail.get("wirelessSsids") or []):
        if not s.get("enabled"):
            continue
        # NS stores an L1 port name as a single 'type number' pair and strips
        # the spaces out of the type token, so emit the SSID base WITHOUT inner
        # spaces (a single space precedes the index). This keeps the name used
        # for the L1 link and the L2 segment identical to NS's stored form, so
        # the L2 binding actually lands on the port.
        base = _sanitise_port_token(str(s.get("name") or "")).replace(" ", "")
        base = base or "wlan"
        port = f"{base} {idx}"
        idx += 1
        if port in seen:
            continue
        seen.add(port)
        vid = s.get("defaultVlanId")
        if s.get("useVlanTagging") and vid is not None:
            try:
                seg = f"Vlan{int(vid)}"
            except (TypeError, ValueError):
                seg = native_seg
        else:
            seg = native_seg
        out.append((port, seg))
    return out


def _add_switch_l3(model: NSModel, sw_name: str,
                   ifaces: List[Dict[str, Any]], counts: Dict[str, int]) -> None:
    """Add an L3 switch's routed SVIs (from switch/routing/interfaces): one SVI
    per VLAN with its IP, self-bound (RULE 15), and a VRF (l3_instance) rename
    when the interface carries one (Catalyst-on-Meraki)."""
    for ri in ifaces:
        vid = ri.get("vlanId")
        if vid is None:
            continue
        svi = normalise_port_name(f"Vlan {int(vid)}")
        model.virtual_ports.append(NSVirtualPort(sw_name, svi, vlan_id=int(vid)))
        model.l2_segments_svi.append(NSL2Segment(sw_name, svi, [f"Vlan{int(vid)}"]))
        ip, subnet = ri.get("interfaceIp"), ri.get("subnet")
        if ip and subnet:
            model.ip_assignments.append(
                NSIPAssignment(sw_name, svi, [f"{ip}/{_subnet_prefix(subnet)}"]))
            counts["ip_assignments"] += 1
        vrf = ri.get("vrfName")
        if not vrf and isinstance(ri.get("vrf"), dict):
            vrf = ri["vrf"].get("name")
        if vrf:
            model.vrf_renames.append((sw_name, svi, str(vrf)))
            counts["vrf_instances"] = counts.get("vrf_instances", 0) + 1


def _add_port_channels(model: NSModel, detail: Dict[str, Any],
                       name_of: Dict[str, str], counts: Dict[str, int]) -> None:
    """Build NS port-channels from switch link-aggregation groups. A group may
    span ports on several switches; each switch's members become one
    Port-channel on that device."""
    for i, agg in enumerate(detail.get("switchLinkAggregations") or [], start=1):
        by_switch: Dict[str, List[str]] = {}
        for m in (agg.get("switchPorts") or []):
            serial, pid = m.get("serial"), m.get("portId")
            if serial in name_of and pid is not None:
                by_switch.setdefault(name_of[serial], []).append(
                    normalise_port_name(f"GigabitEthernet 1/0/{pid}"))
        for sw_name, ports in by_switch.items():
            if not ports:
                continue
            model.port_channels.append(
                NSPortChannel(sw_name, sorted(set(ports)), f"Port-channel {i}"))
            counts["port_channels"] = counts.get("port_channels", 0) + 1


def _annotate_ha_and_uplinks(export: "MerakiExport", name_of: Dict[str, str],
                             model: NSModel) -> List[str]:
    """Fold MX HA (warmSpare) roles and live WAN uplink status into device
    routing attributes. Returns human-readable notes for the report."""
    notes: List[str] = []

    def _append_attr(serial: str, text: str) -> None:
        nsname = name_of.get(serial)
        if not nsname or nsname not in model.devices:
            return
        d = model.devices[nsname]
        d.routing_attribute = (d.routing_attribute + " | " + text).strip(" |")

    for nid, detail in export.network_details.items():
        ws = detail.get("applianceWarmSpare")
        if isinstance(ws, dict) and ws.get("enabled"):
            prim, spare = ws.get("primarySerial"), ws.get("spareSerial")
            if prim:
                _append_attr(prim, "HA: primary")
            if spare:
                _append_attr(spare, "HA: spare")
            if prim and spare:
                notes.append(f"MX HA pair: primary={prim} spare={spare}")

    for st in (export.uplink_statuses or []):
        serial = st.get("serial")
        role = (st.get("highAvailability") or {}).get("role")
        ups = []
        for u in (st.get("uplinks") or []):
            iface, status = u.get("interface"), u.get("status")
            if iface:
                ups.append(f"{iface}:{status}" if status else iface)
        bits = []
        if role:
            bits.append(f"HA-role={role}")
        if ups:
            bits.append("WAN " + ",".join(ups))
        if serial and bits:
            _append_attr(serial, "; ".join(bits))
    return notes


def _switch_port_plan(export: "MerakiExport", serial: str) -> Tuple[List[str], Optional[str]]:
    """From switch ports/statuses: (connected non-uplink access portIds in order,
    the uplink portId). Empty/None when no live status (caller falls back to the
    config-order heuristic)."""
    statuses = export.switch_port_statuses.get(serial) or []
    access = [str(s.get("portId")) for s in statuses
              if s.get("status") == "Connected" and not s.get("isUplink")
              and s.get("portId") is not None]
    uplink = next((str(s.get("portId")) for s in statuses if s.get("isUplink")), None)
    return access, uplink


def _add_vpn_links(export: "MerakiExport", name_of: Dict[str, str],
                   model: NSModel, counts: Dict[str, int]) -> List[str]:
    """Draw AutoVPN site-to-site tunnels between MX gateways of different
    networks (spoke -> hub) and fold BGP AS into gateway attributes. Logical
    overlay links (Tunnel interfaces), so they cross NS areas like the Internet
    link does."""
    gw_by_net: Dict[str, List[str]] = {}
    for d in export.devices:
        if d.product_type == "appliance" and d.serial in name_of:
            gw_by_net.setdefault(d.network_id, []).append(d.serial)

    notes: List[str] = []
    tnum = 0
    for nid, detail in export.network_details.items():
        bgp = detail.get("applianceVpnBgp")
        if isinstance(bgp, dict) and bgp.get("enabled") and bgp.get("asNumber"):
            for s in gw_by_net.get(nid, []):
                ns = name_of.get(s)
                if ns in model.devices:
                    d = model.devices[ns]
                    d.routing_attribute = (d.routing_attribute + f" | BGP AS{bgp['asNumber']}").strip(" |")

        vpn = detail.get("applianceVpnSiteToSite")
        if not (isinstance(vpn, dict) and vpn.get("mode") == "spoke"):
            continue
        spokes = gw_by_net.get(nid, [])
        for hub in (vpn.get("hubs") or []):
            hubs = gw_by_net.get(hub.get("hubId"), [])
            if not (spokes and hubs):
                continue
            tnum += 1
            a, b = name_of[spokes[0]], name_of[hubs[0]]
            port = normalise_port_name(f"Tunnel {tnum}")
            model.l1_links.append(NSL1Link(a, port, b, port))
            counts["l1_links"] += 1
            counts["vpn_links"] = counts.get("vpn_links", 0) + 1
            notes.append(f"AutoVPN: spoke {a} -> hub {b}")
    return notes


def _norm_mac(mac: Any) -> str:
    """Strip a MAC down to bare hex digits for identity matching."""
    return re.sub(r"[^0-9a-f]", "", str(mac).lower())


def _device_lookup(export: "MerakiExport", name_of: Dict[str, str]) -> Dict[str, str]:
    """Identifier (serial / MAC / name) -> NS device name, for resolving the
    remote end of an LLDP/CDP neighbour entry."""
    lut: Dict[str, str] = {}
    for d in export.devices:
        ns = name_of.get(d.serial)
        if not ns:
            continue
        lut[d.serial.lower()] = ns
        if d.mac:
            lut[_norm_mac(d.mac)] = ns
        if d.name.strip():
            lut[d.name.strip().lower()] = ns
        lut[d.display_name.lower()] = ns
    return lut


def _portname(product_type: Optional[str], pid: Any) -> str:
    """Best-effort NS interface name from an LLDP/CDP port id."""
    if pid is None:
        return "GigabitEthernet 0/0"
    s = str(pid).strip()
    if s.isdigit():
        return normalise_port_name(
            f"GigabitEthernet 1/0/{s}" if product_type == "switch"
            else f"GigabitEthernet 0/{s}")
    return normalise_port_name(s)


def _match_neighbor(nb: Dict[str, Any], lut: Dict[str, str]) -> Optional[str]:
    """Resolve an LLDP/CDP neighbour record to a known NS device name."""
    for key in (nb.get("systemName"), nb.get("deviceId")):
        if key and str(key).strip().lower() in lut:
            return lut[str(key).strip().lower()]
    chassis = nb.get("chassisId")
    if chassis and _norm_mac(chassis) in lut:
        return lut[_norm_mac(chassis)]
    return None


def _real_l1_links(
    export: "MerakiExport", devs: List[MerakiDevice], name_of: Dict[str, str],
) -> List[Tuple[str, str, Optional[Tuple[str, str]], str, str, Optional[Tuple[str, str]]]]:
    """Discovered L1 links from per-device LLDP/CDP, matched to known devices.

    Returns tuples (a_ns, a_port, a_sw, b_ns, b_port, b_sw) where ``*_sw`` is the
    (serial, portId) of a switch endpoint (for exact VLAN binding) or None.
    Empty when no neighbour data is available (e.g. virtual/offline devices),
    in which case the caller synthesizes the topology instead.
    """
    lut = _device_lookup(export, name_of)
    ptype_by_serial = {d.serial: d.product_type for d in export.devices}
    ptype_by_ns = {name_of[d.serial]: d.product_type
                   for d in export.devices if d.serial in name_of}
    serial_by_ns = {name_of[d.serial]: d.serial
                    for d in export.devices if d.serial in name_of}
    seen: set = set()
    out: List[Tuple[str, str, Optional[Tuple[str, str]], str, str, Optional[Tuple[str, str]]]] = []
    for d in devs:
        lc = export.lldp_cdp.get(d.serial)
        if not isinstance(lc, dict):
            continue
        a_ns = name_of.get(d.serial)
        if not a_ns:
            continue
        for local_pid, peers in (lc.get("ports") or {}).items():
            if not isinstance(peers, dict):
                continue
            for proto in ("lldp", "cdp"):
                nb = peers.get(proto)
                if not isinstance(nb, dict):
                    continue
                b_ns = _match_neighbor(nb, lut)
                if not b_ns or b_ns == a_ns:
                    continue
                a_port = _portname(ptype_by_serial.get(d.serial), local_pid)
                b_port = _portname(ptype_by_ns.get(b_ns), nb.get("portId"))
                key = tuple(sorted([(a_ns, a_port), (b_ns, b_port)]))
                if key in seen:
                    continue
                seen.add(key)
                a_sw = (d.serial, str(local_pid)) if d.product_type == "switch" else None
                b_sw = ((serial_by_ns[b_ns], str(nb.get("portId")))
                        if ptype_by_ns.get(b_ns) == "switch" and nb.get("portId") is not None
                        else None)
                out.append((a_ns, a_port, a_sw, b_ns, b_port, b_sw))
                break  # one link per local port (prefer LLDP over CDP)
    return out


# Safety cap: never expand a trunk to more than this many VLAN L2 segments
# (a real 'all' trunk would otherwise emit up to 4094 entries).
_TRUNK_VLAN_CAP = 64


def _parse_vlan_spec(spec: Any) -> Optional[List[int]]:
    """Parse a Meraki VLAN spec ('1,10,20-30', '1-4094', 'all', 5) into a sorted
    list of VLAN ids. Returns None for 'all'/'' (meaning "every VLAN")."""
    if spec is None:
        return None
    if isinstance(spec, int):
        return [spec]
    s = str(spec).strip().lower()
    if s in ("", "all", "1-4094"):
        return None
    out: List[int] = []
    for part in s.split(","):
        part = part.strip()
        if "-" in part:
            lo, _, hi = part.partition("-")
            try:
                out.extend(range(int(lo), int(hi) + 1))
            except ValueError:
                continue
        elif part.isdigit():
            out.append(int(part))
    return sorted(set(out)) or None


def _network_vlan_universe(
    export: "MerakiExport", devs: List[MerakiDevice], detail: Dict[str, Any],
) -> List[int]:
    """All VLAN ids that actually exist in a network (appliance VLANs + switch
    access/native VLANs + switch SVIs), so a trunk carrying 'all' binds the real
    set instead of 4094 phantom VLANs."""
    vlans: set = set()
    for v in (detail.get("applianceVlans") or []):
        try:
            vlans.add(int(v.get("id")))
        except (TypeError, ValueError):
            pass
    for d in devs:
        for p in (export.switch_ports.get(d.serial) or []):
            try:
                vlans.add(int(p.get("vlan")))
            except (TypeError, ValueError):
                pass
        for ri in (export.switch_routing.get(d.serial) or []):
            try:
                vlans.add(int(ri.get("vlanId")))
            except (TypeError, ValueError):
                pass
    vlans.discard(0)
    return sorted(vlans) or [1]


def _port_native(port: Optional[Dict[str, Any]]) -> int:
    """A switch port's native (untagged) VLAN id, defaulting to 1."""
    try:
        return int((port or {}).get("vlan"))
    except (TypeError, ValueError):
        return 1


def _port_vlans(port: Dict[str, Any],
                universe: Optional[List[int]] = None) -> List[str]:
    """Derive the NS VLAN list for a switch port from its Dashboard config.

    Access port -> its access VLAN. Trunk -> native VLAN plus every allowed VLAN
    (``allowedVlans`` parsed; ``'all'`` expands to the network's real VLAN
    universe, capped at ``_TRUNK_VLAN_CAP``). Falls back to Vlan1.
    """
    try:
        native = int(port.get("vlan"))
    except (TypeError, ValueError):
        native = 1

    if str(port.get("type")) != "trunk":
        return [f"Vlan{native}"]

    allowed = _parse_vlan_spec(port.get("allowedVlans"))
    if allowed is None:  # 'all' -> the network's real VLANs
        allowed = list(universe or [native])
    vids = sorted({native, *allowed})[:_TRUNK_VLAN_CAP]
    return [f"Vlan{v}" for v in vids]
