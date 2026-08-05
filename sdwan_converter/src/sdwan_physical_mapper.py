# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""Build the *physical underlay* NSModel from a Cisco Catalyst SD-WAN
(vManage) export -- the real cabling/circuits that carry VPN0 transport
traffic PLUS each Edge's real Service VPN LAN side, EXCLUDING the IPsec+BFD
full/partial-mesh tunnel fabric itself (only annotated, see MESH-SHAPE
ANNOTATION below) and the Service VPN CLOUD abstraction (see
``sdwan_logical_mapper`` for that overlay).

Node source:
  * ``devices`` -- every vManage-managed device (``vmanage`` / ``vsmart`` /
    ``vbond`` / ``vedge``). The device's site becomes the NS area -- named
    after its reported ``site-name`` (``br1``), falling back to ``site<id>``
    (see ``sdwan_topology.build_site_area_map``); ``personality`` decides the
    tier row (control-plane above the data-plane Edge).

L1 links -- OBSERVED (preferred) with an INFERRED fallback, in TWO PASSES,
mirroring ``catc_converter``'s "observed cabling first, subnet-inference
supplement second" pattern:

  PASS 1 (preferred, OBSERVED) -- vManage's device/interface APIs return only
  per-device operational STATE (which VPN0 physical port carries which
  IP/colour); by themselves they carry no cable/LLDP-style adjacency between
  two ports, unlike NDFC's or Catalyst Center's observed ``physical-topology``.
  However, a real physical vManage MAY separately expose observed LLDP/CDP
  neighbor adjacency (``/dataservice/device/lldp/neighbors`` and
  ``/dataservice/device/cdp/neighbors`` -- see ``fetch_from_vmanage.py``). When
  present, this pass resolves each Edge's LLDP/CDP neighbor entries to another
  SD-WAN-managed device (see ``sdwan_topology.resolve_neighbor_device``) and
  draws a REAL L1 link with the two real port names -- de-duplicated one cable
  per physical port, LLDP preferred over CDP when both report the same local
  port (LLDP is universal; CDP is IOS-XE/cEdge-only). **This LLDP/CDP field
  schema is UNVERIFIED against a live physical vManage** -- see the module-level
  caveat in ``sdwan_topology.py`` and the README.

  PASS 2 (fallback, INFERRED) -- for every VPN0 physical interface Pass 1 did
  NOT already resolve into an observed link (this is the ONLY path exercised
  by the one live vManage this repo could reach -- a CML-simulated lab whose
  vManage returns no LLDP/CDP data at all), the shared WAN transport circuit
  is inferred the same way ``config_converter`` / ``catc_converter`` infer L3
  reachability across unmanaged switching: every remaining SD-WAN Edge's VPN0
  **physical** interface (Gi/Te/... ; Tunnel and other VPN0 pseudo-interfaces
  are excluded, and are drawn in neither diagram) is grouped by its
  ``transport colour`` ALONE. Each group becomes ONE gray cloud device named
  after that colour (``biz-internet``, ``mpls``) that every member Edge links
  to with its real physical port + IP (a star, not a mesh) -- exactly the
  shared-L2-segment convention already used for inferred underlay links
  elsewhere in this repo. The IPv4 subnet is deliberately NOT part of the
  identity: a TLOC colour IS the transport's identity in SD-WAN, and one
  colour routinely spans several access subnets that are the same WAN as far
  as the fabric is concerned, so they merge into one cloud carrying all of
  them. A colour with only one member still gets its own single-member cloud
  (representing that Edge's own dedicated WAN access circuit).

  If Pass 1 finds zero observed links anywhere, the result is IDENTICAL to
  the previous inference-only behaviour (set ``enable_observed_l1_links`` to
  ``false`` in the config to force pass-2-only unconditionally).

L2 SEGMENTS -- each inferred transport cloud carries ONE L2 segment, named
after the transport colour (``biz-internet``, ``mpls``), on its own ``Dummy N``
ports and on no port of any other device. Without it every Edge-to-cloud link
would be its own 2-node broadcast domain and the Underlay L2/L3 diagrams would
carry no meaningful subnet; with it, the Edges sharing a circuit land in one L3
broadcast domain. The arrangement is deliberately ASYMMETRIC -- exactly as in
``sdwan_logical_mapper`` -- because Network Sketcher treats any port carrying
an L2 segment as an L2 switchport and drops it from the L3 interface table, so
segmenting the Edge's physical port would make its ``add ip_address_bulk``
fail. There is exactly ONE cloud per colour, so the colour is an unambiguous
segment name even for a cloud spanning several access subnets. Each cloud
additionally carries ONE ``Dummy 0`` port modelled as an SVI and self-bound to
that same colour segment (NS RULE 15), with no L1 link and no IP -- it exists
only to give the segment an L3/SVI anchor on the cloud. Carrying no IP is also
what makes a multi-subnet cloud coherent: there is no single "cloud subnet" to
anchor and the export reports no address for the cloud itself, so none is
invented -- the member Edge ports keep their real IPs and every subnet of the
colour still appears on the L3 diagram from those.

MESH-SHAPE ANNOTATION -- when the export carries OBSERVED live BFD
tunnel-session state (``idx.bfd_sessions``, OPTIONAL), each Pass 2 transport
cloud's label additionally gains a ``(Full Mesh)`` / ``(Hub-and-Spoke)`` /
``(Partial Mesh)`` suffix -- ``biz-internet (Partial Mesh)`` -- classified by
``sdwan_topology.classify_mesh_shape`` from that cloud's OWN member Edges,
which since the colour-only merge means every Edge on the colour. Clouds with
fewer than 3 member Edges are skipped. The drawing itself never changes: this
only says how densely the SD-WAN fabric meshes across the Edges attached to
that transport.

SERVICE VPN LAN SIDE -- below the Edges, this underlay draws the very same LAN
side the overlay does, via the shared ``sdwan_lan_side`` helper both mappers
call: every REAL Service VPN PHYSICAL interface (``GigabitEthernet 3``, ...)
L1-linked DOWN to one synthetic per-Edge ``Dummy_<n>`` switch with its
reported IP, every Service VPN Loopback as an unconnected virtual port, and
the VPN's VRF on both ends of each LAN link and on each Loopback. Sharing the
plan is what guarantees the two diagrams place identical switches under
identical Edges, so they can be read side by side.

TRANSPORT VRF -- the Edges' VPN0 PHYSICAL transport ports additionally carry
VPN 0 itself as an L3 instance (``Transport_vpn0``), built by the same
``sdwan_topology.vrf_label_for_vpn`` / ``vrf_name_format`` path as
``Corporate_vpn10`` from vpn-id 0 and the ``transport_vpn_name`` configuration
value, so the L3 interface table separates transport from the Service VPNs.
Everything else about those ports is unchanged: same IP, same L1 link. The
transport CLOUDS are deliberately left out of it -- their ``Dummy N`` and
``Dummy 0`` ports carry the colour L2 segment and no VRF, mirroring the
overlay, which likewise never writes a VRF onto its waypoint clouds.

CONTROL PLANE -- the controllers are connected too, through two synthetic hops
rather than a fabricated adjacency. vManage reports each Edge's live control
connections (``idx.control_connections``, OPTIONAL), giving the Edge's LOCAL
transport colour and the controller's transport address per session; resolving
that colour BY ADDRESS through ``control/waninterface`` says which inferred
transport cloud carries the control plane. It does NOT say how far the
controllers are: they sit on their own subnet, disjoint from every Edge
transport subnet, and no route table is exposed. So a Router (``Dummy_l3``)
stands in for the unknown routed hop, linked DOWN to the ONE transport segment
the evidence most strongly points at (observed rows, then controllers reached,
then member Edges, then name) and UP to a Switch (``Dummy_l2``) standing in for
the segment the controllers share; every controller links to that switch, which
carries one ``dummy_l2`` L2 segment on ALL its own ports. Ports on both are
pseudo ports numbered from ``Dummy 0`` and neither carries an IP -- but each
controller's own VPN 0 port keeps its reported address (and NO VRF: a
controller is a Server-role appliance, not a routing node), so the shared
segment is a real subnet in the L3
diagram. The whole area is lifted ABOVE the transport clouds and its internal
order reversed (see ``_promote_controller_area_to_top``), so the path reads
downwards like an Edge's own uplink. Without usable control-connection data -- or with
``enable_controller_control_plane_links`` set to false -- the controllers stay
unconnected inventory nodes, exactly as before this feature existed.

  Deliberately EXCLUDED from this underlay: the IPsec+BFD tunnel-to-tunnel
  mesh/partial-mesh as LINKS (only the label suffix above uses BFD state), the
  Service VPN CLOUD abstraction -- the synthetic ``Vpn <id>`` ports and the
  per-VPN waypoint clouds, see ``sdwan_logical_mapper`` -- OMP route state, and
  a controller's VPN 512 out-of-band management port (a genuinely separate
  network -- unlike the control traffic itself, which rides VPN 0 transport).
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from . import sdwan_lan_side as lan_side
from . import sdwan_stencil_mapper as sm
from . import sdwan_topology as topo
from .sdwan_stencil_mapper import StencilMapping
from .ns_model import (
    NSDevice, NSIPAssignment, NSL1Link, NSL2Segment, NSModel, NSVirtualPort,
    build_area_layout, normalise_port_name,
)

# Tier rows for the underlay. The controllers' area is promoted to its own row
# ABOVE the transport clouds (see _promote_controller_area_to_top), so its
# internal order runs the OPPOSITE way round from a site's: the servers sit at
# the top and the synthetic routed hop at the bottom, nearest the clouds it
# links DOWN to.
_ROW_CONTROL = -2     # vManage / vSmart / vBond
_ROW_CONTROL_L2 = -1  # synthetic segment the controllers share
_ROW_CONTROL_L3 = 0   # synthetic routed hop, BELOW the control-plane segment
_ROW_EDGE = 1       # SD-WAN Edge (cEdge / vEdge)
_ROW_LAN_DUMMY = 4  # RULE 0 Access tier -- the LAN switch drawn BELOW its Edge

_AREA_WAN = "wan"   # raw waypoint area name (promoted to 'wan_wp_' by build_area_layout)

# Light-gray colour for an INFERRED node (the shared VPN0 transport segment).
# Uses the Cloud stencil (see sdwan_stencil_mapper.map_logical) so the
# attribute builder classifies it as a WayPoint -- consistent with every
# other converter's cross-area shared-segment convention.
_SEG_COLOR = (200, 200, 200)

# The synthetic per-Edge Service VPN LAN switch: the same light gray, for the
# same reason (a synthesised placeholder with no vManage counterpart), and the
# same colour the overlay gives the very same device. Its ports are pseudo
# ports, so -- unlike the Edges' real VPN0 uplinks -- no physical
# speed/duplex/media is invented for them.
_LAN_DUMMY_COLOR = (200, 200, 200)
_LAN_DUMMY_PORT_INFO = ("Unknown", "Unknown", "Unknown")

# One extra port per transport cloud, modelled as an SVI and self-bound to that
# cloud's own transport-colour L2 segment (NS RULE 15). Synthesised: no vManage
# counterpart, no L1 link and no IP. Safe against the cloud's L1 'Dummy N'
# ports, which are numbered from 1 (see the member loop below).
_CLOUD_SVI_PORT = "Dummy 0"

# Display name for VPN 0 in the transport VRF label, resolved through the same
# 'vrf_name_format' path as every Service VPN -> 'Transport_vpn0'. Taken from
# the configuration rather than the export's own 'vpn_names', whose VPN 0
# entry is typically a placeholder ('VPN0') that would render as 'VPN0_vpn0'.
# Blank it in the configuration to fall back to a bare 'vpn0'.
_DEFAULT_TRANSPORT_VPN_NAME = "Transport"

# The two synthetic control-plane devices drawn ABOVE the controllers, and the
# L2 segment the lower one carries on every one of its ports. Both are the same
# light gray as every other synthesised placeholder, and both number their
# pseudo ports from 'Dummy 0' -- unlike the transport clouds, which reserve
# 'Dummy 0' for the RULE 15 SVI and start their link ports at 'Dummy 1'.
_DEFAULT_CONTROL_L3_NAME = "Dummy_l3"
_DEFAULT_CONTROL_L2_NAME = "Dummy_l2"
_CONTROL_L2_SEGMENT = "dummy_l2"
_CONTROL_DUMMY_COLOR = (200, 200, 200)


def build_physical_model(
    idx,
    cfg: Optional[Dict[str, Any]] = None,
    layout: str = "tier",
) -> Tuple[NSModel, Dict[str, Any]]:
    """Return (NSModel, info) for the physical underlay across all devices."""
    cfg = cfg or {}
    naming = str(cfg.get("device_naming", "hostname") or "hostname")
    unknown_color = str(cfg.get("unknown_color_label", "unknown") or "unknown")
    vrf_fmt = str(cfg.get("vrf_name_format") or topo.DEFAULT_VRF_NAME_FORMAT)
    transport_vpn_name = topo.sanitize(
        str(cfg.get("transport_vpn_name", _DEFAULT_TRANSPORT_VPN_NAME) or ""))

    enable_observed = bool(cfg.get("enable_observed_l1_links", True))
    prefer_lldp = bool(cfg.get("prefer_lldp_over_cdp", True))
    enable_control_plane = bool(cfg.get("enable_controller_control_plane_links", True))
    control_l3_name = str(cfg.get("control_l3_dummy_name") or _DEFAULT_CONTROL_L3_NAME)
    control_l2_name = str(cfg.get("control_l2_dummy_name") or _DEFAULT_CONTROL_L2_NAME)

    model = NSModel()
    mappings: List[StencilMapping] = []
    seen_names: set = set()
    sysip_to_name: Dict[str, str] = {}
    name_to_sysip: Dict[str, str] = {}   # NS device name -> system-ip (mesh annotation)
    edge_site_by_sysip: Dict[str, str] = {}
    host_by_lower: Dict[str, str] = {}   # raw host-name (lowercased) -> NS device name
    counts = {"vmanage": 0, "vsmart": 0, "vbond": 0, "edge": 0, "device": 0,
              "l1_links": 0, "transport_segments": 0, "single_member_segments": 0,
              "multi_subnet_segments": 0,
              "l2_segments": 0, "cloud_svi_ports": 0,
              "ip_assignments": 0, "unresolved_color": 0,
              "observed_links": 0, "observed_links_lldp": 0, "observed_links_cdp": 0,
              "edges_with_neighbor_data": 0, "unresolved_neighbors": 0,
              "mesh_annotated": 0,
              "lan_dummy": 0, "lan_links": 0, "service_vpn_loopbacks": 0,
              "transport_vrf_renames": 0, "lan_vrf_renames": 0,
              "vrf_renames": 0, "vrfs": 0,
              "edges_with_control_data": 0, "control_conn_rows": 0,
              "control_conn_unresolved": 0, "control_dummy_devices": 0,
              "control_uplinks": 0, "control_links": 0, "control_l2_segments": 0,
              "controllers_linked": 0, "controllers_history_only": 0,
              "control_ip_assignments": 0}
    caveats: List[str] = []

    def _unique(name: str) -> str:
        name = topo.sanitize(name) or "device"
        base, i = name, 2
        while name in seen_names:
            name = f"{base}_{i}"
            i += 1
        seen_names.add(name)
        return name

    # ----- devices -> NSDevice --------------------------------------------
    # Area names come from each device's reported ``site-name`` (falling back
    # to ``site<id>``); resolved once, fabric-wide, by the shared helper so
    # the overlay draws the very same area names.
    site_area = topo.build_site_area_map(idx.devices)
    controller_areas: Set[str] = set()  # promoted to the TOP row further down
    for dev in idx.devices:
        personality = topo.device_personality(dev)
        inferred = topo.device_is_inferred_personality(dev) and bool(personality)
        name = _unique(topo.device_display_name(dev, naming))
        st = sm.map_device(
            name=name, personality=personality or "vedge",
            device_model=dev.get("device-model") or "",
            uuid=dev.get("uuid") or "", inferred=inferred,
        )
        mappings.append(st)
        sysip = str(dev.get("system-ip") or "")
        if sysip:
            sysip_to_name[sysip] = name
            name_to_sysip[name] = sysip
        host = str(dev.get("host-name") or "").strip().lower()
        if host:
            host_by_lower[host] = name

        site_id = dev.get("site-id")
        if sysip and site_id not in (None, ""):
            edge_site_by_sysip[sysip] = str(site_id)
        area = (site_area.get(str(site_id).strip(), f"site{site_id}")
                if site_id not in (None, "") else "default")
        row = _ROW_CONTROL if personality in ("vmanage", "vsmart", "vbond") else _ROW_EDGE

        attr_bits: List[str] = []
        if sysip:
            attr_bits.append(f"system-ip {sysip}")
        if site_id not in (None, ""):
            attr_bits.append(f"site-id {site_id}")
        if dev.get("reachability"):
            attr_bits.append(f"reachability {dev['reachability']}")
        if dev.get("uuid"):
            attr_bits.append(f"uuid {dev['uuid']}")

        model.devices[name] = NSDevice(
            name=name, area=area, row=row, stencil=st, is_endpoint=False,
            routing_attribute=" | ".join(attr_bits),
        )
        counts["device"] += 1
        if personality in ("vmanage", "vsmart", "vbond"):
            counts[personality] += 1
            controller_areas.add(area)
        else:
            counts["edge"] += 1

    # ----- per-Edge VPN0 physical-interface port facts ----------------------
    # Scoped to SD-WAN Edges only: a controller's own VPN0 port is not a
    # transport circuit endpoint (it sits on a subnet of its own, off the
    # Edges' transport networks) and is handled by the control-plane pass
    # further down instead. Built ONCE, up front, so both passes below share
    # identical per-port (ip, cidr, network, colour) facts.
    edge_ports: Dict[str, Dict[str, Dict[str, str]]] = defaultdict(dict)
    for dev in idx.devices:
        if topo.device_personality(dev) != "vedge":
            continue
        sysip = str(dev.get("system-ip") or "")
        name = sysip_to_name.get(sysip)
        if not name:
            continue
        wan_ifaces = idx.wan_interfaces.get(sysip, [])
        for itf in topo.physical_wan_interfaces(idx.interfaces.get(sysip, [])):
            ip = itf.get("ip-address") or ""
            cidr = topo.cidr_from_mask(ip, itf.get("ipv4-subnet-mask") or "")
            if not cidr:
                continue
            network = topo.network_of(cidr)
            if not network:
                continue
            color = topo.color_for_ip(wan_ifaces, ip)
            if not color:
                color = unknown_color
                counts["unresolved_color"] += 1
            port = normalise_port_name(itf.get("ifname") or "")
            edge_ports[name][port] = {"ip": ip, "cidr": cidr, "network": network, "color": color}

    # ----- PASS 1 (preferred, OBSERVED) -- LLDP/CDP neighbor adjacency ------
    # Resolves each Edge's neighbor entries into a REAL device-to-device L1
    # link wherever both ends join to an SD-WAN-managed device (see
    # sdwan_topology.resolve_neighbor_device); claims the VPN0 physical ports
    # it uses so Pass 2 below never double-counts them into an inferred
    # segment. A no-op (zero links) on any export with no lldp_neighbors /
    # cdp_neighbors data -- e.g. this repo's own bundled CML-lab sample -- so
    # Pass 2 alone reproduces the pre-existing inference-only behaviour byte
    # for byte. See the module docstring for the UNVERIFIED-schema caveat.
    used_ports: set = set()   # (device, port) already resolved by Pass 1
    if enable_observed:
        _apply_observed_links(
            idx, model, sysip_to_name, host_by_lower, edge_ports,
            used_ports, counts, prefer_lldp=prefer_lldp,
        )

    # ----- PASS 2 (fallback, INFERRED) -- one star segment per COLOUR ------
    # Every VPN0 physical port NOT already resolved into an observed link by
    # Pass 1 (i.e. every one of them, when Pass 1 found nothing) is grouped by
    # its transport COLOUR alone. The IPv4 subnet is deliberately NOT part of
    # the key: a TLOC colour is the transport's identity in SD-WAN, and one
    # colour routinely spans several access subnets that are the same WAN as
    # far as the fabric is concerned.
    groups: Dict[str, List[Tuple[str, str, str]]] = defaultdict(list)
    for name, ports in edge_ports.items():
        for port, info in ports.items():
            if (name, port) in used_ports:
                continue  # already a real OBSERVED link from Pass 1
            groups[info["color"]].append((name, port, info["cidr"]))

    # ----- one gray transport cloud per COLOUR ----------------------------
    # The cloud is NAMED after the colour alone ('biz-internet'), plus a
    # mesh-shape suffix -- "(Full Mesh)" / "(Hub-and-Spoke)" / "(Partial
    # Mesh)" -- whenever OBSERVED live BFD tunnel-session state can classify
    # it (see topo.classify_mesh_shape). The classification spans the same
    # member Edges the cloud does, so it now describes the whole colour. It
    # is an ANNOTATION only: the star drawing (Edge port -> cloud) is
    # identical either way, and no Edge-to-Edge link is ever added.
    bfd_sessions = getattr(idx, "bfd_sessions", {}) or {}
    mesh_shape_by_segment: Dict[str, str] = {}
    # colour -> that cloud's NS device name, and the next free 'Dummy N' port
    # on it -- both needed by the control-plane pass below, which attaches one
    # more link port to one of these clouds.
    seg_by_group: Dict[str, str] = {}
    cloud_next_port: Dict[str, int] = {}
    multi_subnet_clouds: Dict[str, List[str]] = {}
    for color, members in sorted(groups.items()):
        member_sysips = sorted({
            name_to_sysip[devname] for devname, _port, _cidr in members
            if devname in name_to_sysip
        })
        networks = sorted({
            topo.network_of(cidr) or cidr for _devname, _port, cidr in members
        })
        # Below 3 member Edges a "mesh shape" is meaningless (a single pair is
        # trivially a full mesh), so the annotation is skipped entirely.
        mesh_shape = None
        if len(member_sysips) >= 3:
            mesh_shape = topo.classify_mesh_shape(
                edge_sysips=member_sysips,
                edge_site=edge_site_by_sysip,
                bfd_sessions=bfd_sessions,
                color=color,
            )

        label = f"{color} ({mesh_shape})" if mesh_shape else color
        seg_name = _unique(label)
        st = sm.map_logical(
            seg_name, "transport-segment",
            model=f"Inferred VPN0 transport segment (colour={color})",
        )
        mappings.append(st)
        # 'networks' goes LAST: the command builder truncates this cell, and a
        # cloud can now carry an unbounded list of subnets, so the fixed-length
        # facts must come first or a long list would chop them off.
        attr_bits = [f"colour={color}", f"members={len(members)} (inferred)"]
        if mesh_shape:
            mesh_shape_by_segment[color] = mesh_shape
            attr_bits.append(f"mesh={mesh_shape} (observed BFD sessions)")
            counts["mesh_annotated"] += 1
        attr_bits.append(f"networks={','.join(networks)}")
        model.devices[seg_name] = NSDevice(
            name=seg_name, area=_AREA_WAN, row=0, stencil=st, is_endpoint=False,
            default_color=_SEG_COLOR,
            routing_attribute=" | ".join(attr_bits),
        )
        counts["transport_segments"] += 1
        seg_by_group[color] = seg_name
        cloud_next_port[seg_name] = len(members) + 1
        if len(members) == 1:
            counts["single_member_segments"] += 1
        if len(networks) > 1:
            multi_subnet_clouds[seg_name] = networks
            counts["multi_subnet_segments"] += 1
        for i, (devname, port, cidr) in enumerate(sorted(members), start=1):
            cloud_port = f"Dummy {i}"
            model.l1_links.append(NSL1Link(devname, port, seg_name, cloud_port))
            model.ip_assignments.append(NSIPAssignment(device=devname, port=port, cidrs=[cidr]))
            counts["ip_assignments"] += 1
            # Cloud side ONLY (see the module docstring's L2 SEGMENTS block):
            # this binds the cloud's Dummy ports into one broadcast domain so
            # the segment's member Edges share an L3 subnet, while their real
            # physical ports stay routed L3 interfaces that can keep their IP.
            model.l2_segments_phys.append(
                NSL2Segment(device=seg_name, port=cloud_port, vlans=[color]))
            counts["l2_segments"] += 1

        # RULE 15 SVI self-binding -- the cloud's 'Dummy 0' port is bound to
        # the very same colour segment its 'Dummy N' link ports carry above,
        # so the two can never drift apart. No L1 link and no IP: the SVI only
        # gives the colour segment an L3 anchor on the cloud. Carrying no IP
        # is what lets a colour cloud span SEVERAL subnets -- there is no
        # single "the cloud's subnet" to pick, and the source export has no
        # address for the cloud itself, so none is invented. The member Edge
        # ports keep their own real IPs, and NS still renders every subnet of
        # the cloud on L3 from those.
        model.virtual_ports.append(
            NSVirtualPort(device=seg_name, port=_CLOUD_SVI_PORT))
        model.l2_segments_svi.append(
            NSL2Segment(device=seg_name, port=_CLOUD_SVI_PORT, vlans=[color]))
        counts["cloud_svi_ports"] += 1

    # ----- VPN 0 TRANSPORT VRF -- Edge side only ---------------------------
    # Every Edge VPN0 physical port drawn above (Pass 1 or Pass 2 -- every
    # port in edge_ports ends up in exactly one of them) carries the Transport
    # VPN as its L3 instance, so the L3 interface table separates transport
    # from the Service VPNs instead of pooling it into an unnamed default.
    # The name goes through the very same vrf_label_for_vpn/'vrf_name_format'
    # path as 'Corporate_vpn10', with vpn-id 0 and the configured VPN 0 name.
    # The transport CLOUDS get none of this: their 'Dummy N'/'Dummy 0' ports
    # carry the colour L2 segment instead, exactly as the overlay leaves its
    # own waypoint clouds out of every VRF.
    transport_vrf = topo.vrf_label_for_vpn(
        0, {"0": transport_vpn_name} if transport_vpn_name else {}, vrf_fmt)
    for devname in sorted(edge_ports):
        for port in sorted(edge_ports[devname]):
            model.vrf_renames.append((devname, port, transport_vrf))
            counts["transport_vrf_renames"] += 1
            counts["vrf_renames"] += 1

    # ----- SERVICE VPN LAN SIDE -- shared verbatim with the overlay --------
    # Planned by sdwan_lan_side from the export alone, so this underlay places
    # the SAME 'Dummy_<n>' switches, under the SAME Edges, with the SAME VRF
    # names as sdwan_logical_mapper -- the two diagrams are meant to be read
    # side by side.
    lan_plan = lan_side.plan_service_vpn_lan_side(idx, cfg)
    for lan in lan_plan.edges:
        name = sysip_to_name.get(str(lan.device.get("system-ip") or ""))
        if not name or name not in model.devices:
            continue
        lan_counts = lan_side.apply_edge_lan_side(
            model, mappings, name, model.devices[name].area, lan,
            unique_name=_unique, row=_ROW_LAN_DUMMY, color=_LAN_DUMMY_COLOR,
            port_info=_LAN_DUMMY_PORT_INFO,
        )
        counts["lan_dummy"] += lan_counts.lan_dummy
        counts["lan_links"] += lan_counts.lan_links
        counts["service_vpn_loopbacks"] += lan_counts.loopback_ports
        counts["ip_assignments"] += lan_counts.ip_assignments
        counts["lan_vrf_renames"] += lan_counts.vrf_renames
        counts["vrf_renames"] += lan_counts.vrf_renames

    # ----- CONTROL PLANE -- the controllers' path to the transport ---------
    # Joins each Edge's observed control connections to the transport segments
    # inferred above, then draws the two synthetic hops that carry the
    # controllers down from them (see the module docstring). Entirely
    # best-effort: without usable control-connection data the controllers stay
    # unconnected inventory nodes, exactly as before this feature existed.
    control_plane_detail: List[str] = []
    if enable_control_plane:
        control_plane_detail = _apply_control_plane_links(
            idx, model, mappings, sysip_to_name, edge_ports, seg_by_group,
            cloud_next_port, unique_name=_unique, counts=counts,
            l3_name=control_l3_name, l2_name=control_l2_name,
        )
    counts["vrfs"] = len({vrf for _dev, _port, vrf in model.vrf_renames})
    counts["l1_links"] = len(model.l1_links)  # every pass + LAN + control plane

    model.areas, model.area_to_devices = build_area_layout(
        model.devices, model.l1_links, layout=layout,
    )
    l3_less_moved = _order_l3_less_areas_last(model)
    controllers_promoted = _promote_controller_area_to_top(model, controller_areas)

    # ----- caveats ----------------------------------------------------------
    if not enable_observed:
        caveats.append(
            "MODEL -- 'enable_observed_l1_links' is set to false in the "
            "configuration: Pass 1 (LLDP/CDP observed adjacency) was skipped "
            "entirely and every VPN0 physical interface was grouped by the "
            "Pass 2 per-colour inference below, regardless of whether "
            "the export actually carried lldp_neighbors/cdp_neighbors data."
        )
    elif counts["observed_links"]:
        caveats.append(
            f"OBSERVED -- {counts['observed_links']} L1 link(s) "
            f"(lldp={counts['observed_links_lldp']}, cdp={counts['observed_links_cdp']}) "
            "were drawn from real LLDP/CDP neighbor adjacency reported by "
            f"vManage for {counts['edges_with_neighbor_data']} Edge(s) -- the "
            "PREFERRED source for this underlay's L1 links, taking priority "
            "over the per-colour inference below for every VPN0 "
            "physical port they cover (LLDP preferred over CDP when both "
            "report the same local port). The exact LLDP/CDP JSON field names "
            "this parser looks for are a BEST-EFFORT convention (UNVERIFIED "
            "against a real physical vManage) -- see sdwan_topology.py and "
            "the README before fully trusting these links on a new platform."
        )
    else:
        caveats.append(
            "MODEL -- vManage's LLDP/CDP neighbor endpoints "
            "(/dataservice/device/lldp/neighbors, /dataservice/device/cdp/neighbors) "
            "returned no usable adjacency for any SD-WAN Edge in this export "
            "(either the export has no lldp_neighbors/cdp_neighbors data at "
            "all -- the case for every export validated so far, including the "
            "bundled sample -- or none of it could be joined to a known "
            "device/port). Pass 1 therefore drew zero OBSERVED links; every "
            "VPN0 physical interface below was grouped by the Pass 2 "
            "per-colour INFERENCE instead, exactly as before this "
            "feature existed."
        )
    if counts["unresolved_neighbors"]:
        caveats.append(
            f"MODEL -- {counts['unresolved_neighbors']} LLDP/CDP neighbor "
            "entr(y/ies) were found but could not be joined to a known "
            "SD-WAN-managed device (host-name/system-ip) and were skipped -- "
            "either a real but unmanaged/non-SD-WAN neighbour, or this "
            "parser's best-effort field-name guesses did not match the "
            "platform's actual schema."
        )
    caveats.append(
        "INFERRED -- every VPN0 PHYSICAL interface NOT already resolved into "
        "an OBSERVED link above is grouped by its TRANSPORT COLOUR ALONE into "
        "ONE gray cloud named after that colour ('biz-internet'), which every "
        "member links to (a star, not a mesh). The IPv4 subnet is NOT part of "
        "the identity: a TLOC colour is the transport's identity in SD-WAN, "
        "so several access subnets of one colour merge into a single cloud "
        "carrying all of them. A colour with only one member still gets its "
        "own single-member cloud (that Edge's dedicated WAN access circuit)."
    )
    if counts["l2_segments"]:
        caveats.append(
            f"MODEL -- {counts['l2_segments']} L2 segment entr(y/ies) named "
            "after the transport COLOUR ('biz-internet') are written onto the "
            "inferred cloud's own 'Dummy N' link ports and onto NO Edge port. "
            "That segment is what makes this underlay's L2/L3 diagrams "
            "meaningful: it binds each cloud's ports into ONE broadcast "
            "domain, so the Edges sharing that circuit share an L3 subnet "
            "instead of each link becoming its own 2-node domain. It is "
            "deliberately ASYMMETRIC (cloud side only) because Network "
            "Sketcher treats any port carrying an L2 segment as an L2 "
            "switchport and drops it from the L3 interface table, which would "
            "make the Edge port's IP address unassignable. There is exactly "
            "ONE cloud per colour, so the colour is an unambiguous segment "
            "name even when the cloud spans several access subnets."
        )
    if counts["cloud_svi_ports"]:
        caveats.append(
            f"SYNTHETIC -- {counts['cloud_svi_ports']} '{_CLOUD_SVI_PORT}' "
            "SVI port(s), one per inferred transport cloud, are created and "
            "self-bound (Network Sketcher RULE 15) to that cloud's OWN "
            "transport-colour L2 segment. Each has NO vManage counterpart, NO "
            "L1 link and NO IP address: it exists only to give the colour "
            "segment an L3/SVI anchor on the cloud. Carrying no IP is also "
            "what lets a cloud span SEVERAL access subnets -- there is no "
            "single 'the cloud's subnet' to anchor, and the export reports no "
            "address for the cloud itself, so none is invented; the member "
            "Edge ports keep their own real IPs and every subnet of the cloud "
            "still appears on L3 from those. It cannot collide with the "
            "cloud's L1 link ports, which are numbered 'Dummy 1' upwards."
        )
    if counts["lan_vrf_renames"]:
        caveats.append(
            "MODEL -- this underlay is NOT VPN-0-only: alongside the WAN "
            "transport above, it also draws each Edge's SERVICE VPN LAN side "
            f"-- {counts['lan_links']} physical Service VPN interface(s) "
            f"linked DOWN to {counts['lan_dummy']} per-Edge LAN switch(es), "
            f"{counts['service_vpn_loopbacks']} Service VPN Loopback(s), and "
            "the VPN's VRF written onto every one of those ports "
            f"({counts['lan_vrf_renames']} 'rename l3_instance' entr(y/ies)). "
            "It is the same LAN side the OVERLAY draws, planned by the shared "
            "sdwan_lan_side helper both mappers call, so the two diagrams "
            "place identical 'Dummy_<n>' switches under identical Edges with "
            "identical VRF names. What stays exclusive to the overlay is the "
            "Service VPN CLOUD abstraction: the synthetic 'Vpn <id>' ports "
            "and the per-VPN waypoint clouds are NOT drawn here."
        )
    if counts["transport_vrf_renames"]:
        caveats.append(
            "MODEL -- the Edges' VPN 0 PHYSICAL transport interfaces are put "
            f"in the '{transport_vrf}' L3 instance "
            f"({counts['transport_vrf_renames']} 'rename l3_instance' "
            "entr(y/ies)), so VPN 0 -- the SD-WAN Transport VPN -- is drawn "
            "as its own routing domain instead of being pooled into an "
            "unnamed default alongside the Service VPNs. The name is built by "
            "the same vrf_label_for_vpn/'vrf_name_format' path as "
            "'Corporate_vpn10', from vpn-id 0 and the 'transport_vpn_name' "
            "configuration value ('Transport' by default; blank it for a bare "
            "'vpn0'). It is deliberately NOT taken from the export's own "
            "vpn_names entry for VPN 0, which is typically a placeholder "
            "('VPN0') that would render as 'VPN0_vpn0'. Everything else about "
            "those ports is unchanged: same IP, same L1 link to the transport "
            "cloud."
        )
        caveats.append(
            "MODEL -- the VRF is written on the EDGE side only: the inferred "
            "transport clouds' own 'Dummy N' link ports and their "
            f"'{_CLOUD_SVI_PORT}' SVI carry the transport-colour L2 segment "
            "and NO 'rename l3_instance' at all. This mirrors the overlay, "
            "where the Edges and the LAN switches get the VRF but the Service "
            "VPN waypoint clouds never do, and it is also what keeps the "
            "cloud ports valid L2 switchports for the L2/L3 renderer."
        )
    if counts["lan_dummy"]:
        caveats.append(
            f"SYNTHETIC -- {counts['lan_dummy']} per-Edge LAN switch(es) "
            f"carrying {counts['lan_links']} LAN link(s) DO NOT EXIST in the "
            "vManage data: each is one invented aggregation point per Edge so "
            "that Edge's real Service VPN LAN ports terminate somewhere below "
            "it (separated by VRF) instead of floating unconnected. Whatever "
            "LAN switching actually exists behind those ports is invisible to "
            "vManage. They are drawn light gray -- this repo's palette colour "
            "for a synthesised placeholder, the same colour as the inferred "
            "transport clouds above -- on the Access tier row below their "
            "Edge, and their pseudo ports report Unknown speed/duplex/media "
            "rather than an invented physical value. An Edge whose only "
            "Service VPN interfaces are Loopbacks gets no LAN switch."
        )
    if counts["service_vpn_loopbacks"]:
        caveats.append(
            f"MODEL -- {counts['service_vpn_loopbacks']} Service VPN "
            "Loopback interface(s) are drawn as UNCONNECTED virtual ports (IP "
            "+ VRF only, no L1 link and no L2 segment), per Network Sketcher "
            "RULE 15, which names Loopback as an explicit exception to "
            "SVI/L2-segment binding. They appear on the L3 diagram only, and "
            "are the same Loopbacks the overlay draws."
        )
    if counts["single_member_segments"]:
        caveats.append(
            f"INFERRED -- {counts['single_member_segments']} of "
            f"{counts['transport_segments']} transport cloud(s) have exactly "
            "ONE member Edge (no other Edge reported an unresolved VPN0 port "
            "on that colour) and are drawn as a single-member cloud rather "
            "than omitted, so that Edge's WAN circuit is still represented."
        )
    if multi_subnet_clouds:
        detail = "; ".join(
            f"{name}: {', '.join(nets)}"
            for name, nets in sorted(multi_subnet_clouds.items())
        )
        caveats.append(
            f"INFERRED -- {counts['multi_subnet_segments']} transport "
            "cloud(s) carry MORE THAN ONE IPv4 access subnet, because the "
            f"grouping key is the colour alone: {detail}. Every listed subnet "
            "is real (each comes from a member Edge's own VPN0 address); the "
            "cloud is one L2 domain per colour, so the L3 diagram shows all "
            "of that colour's subnets on the one cloud. If your fabric runs "
            "genuinely separate circuits on the same colour and you need them "
            "drawn apart, they are not distinguishable from this export by "
            "subnet alone -- give them distinct TLOC colours in vManage."
        )
    if counts["unresolved_color"]:
        caveats.append(
            f"INFERRED -- {counts['unresolved_color']} VPN0 physical "
            f"interface(s) could not be matched to a transport colour via "
            "control/waninterface (e.g. the fetch skipped it, or the colour "
            "was not yet provisioned) and were grouped under the "
            "'unknown_color_label' configuration value instead."
        )
    if l3_less_moved:
        caveats.append(
            f"LAYOUT -- {l3_less_moved} area(s) hold no IP-bearing (L3) "
            "interface at all (e.g. a site whose Edges reported no usable VPN 0 "
            "address) and were moved to the END of their "
            "'add area_location' row. This works around a Network Sketcher "
            "engine bug: the all-areas L3 renderer's calculate_area_offset "
            "sums the widths of every area PRECEDING each area in its row, "
            "but only areas that carry L3 content appear in that width "
            "lookup, so an L3-less area anywhere but last aborts the export "
            "with a KeyError. The L1 diagram is unaffected -- such an area "
            "has no L1 link to any other area either."
        )
    if controllers_promoted:
        caveats.append(
            "LAYOUT -- the controllers' area was lifted out of the site row "
            "into a row of its own at the TOP of 'add area_location', above "
            "the transport clouds, and the order inside it was reversed "
            "(controllers on top, then the synthetic switch, then the "
            "synthetic router) so its control-plane path runs DOWNWARDS to "
            "the clouds, the way each Edge's own transport uplink does. Only "
            "the ROW is emitted: Network Sketcher assigns each row's starting "
            "COLUMN itself, from the rendered width of the areas on it, so the "
            "horizontal alignment of the three rows is data-dependent."
        )
    if mesh_shape_by_segment:
        detail = ", ".join(f"{k}={v}" for k, v in sorted(mesh_shape_by_segment.items()))
        caveats.append(
            f"MESH-SHAPE ANNOTATION -- {counts['mesh_annotated']} transport "
            "segment(s) had OBSERVED live BFD tunnel-session data for their "
            f"member Edges and were classified as: {detail} (see "
            "sdwan_topology.classify_mesh_shape; based on state='up' BFD "
            "sessions between distinct-site member Edges). This is a LABEL "
            "SUFFIX ONLY -- the star drawing is unchanged and no Edge-to-Edge "
            "link is added. It reflects the OBSERVED live fabric, not policy "
            "intent, so a down session makes the fabric look sparser than "
            "designed. Segments with fewer than 3 member Edges are never "
            "annotated (a mesh shape is meaningless for a single pair)."
        )
    elif bfd_sessions:
        caveats.append(
            "MESH-SHAPE ANNOTATION SKIPPED -- BFD session data was present in "
            "this export but no transport segment could be classified (fewer "
            "than 3 member Edges, or no 'up' session between distinct-site "
            "members of any one segment)."
        )
    caveats.append(
        "OUT OF SCOPE (by design) -- the IPsec+BFD tunnel-to-tunnel mesh / "
        "partial-mesh itself and OMP route state are NOT drawn as links in "
        "this underlay; observed BFD state is used ONLY for the mesh-shape "
        "label suffix above. The Service VPN CLOUD abstraction (one waypoint "
        "cloud per VPN, reached through a synthetic 'Vpn <id>' port) is out "
        "of scope here too -- see sdwan_logical_mapper for the Service VPN "
        "OVERLAY, which is scoped to exactly that."
    )
    if not enable_control_plane:
        caveats.append(
            "MODEL -- 'enable_controller_control_plane_links' is set to false "
            "in the configuration: the vManage / vSmart / vBond controllers "
            "are drawn as devices (for inventory/site context) with NO "
            "connectivity at all, regardless of whether the export carried "
            "control_connections data."
        )
    elif counts["control_dummy_devices"]:
        caveats.append(
            f"SYNTHETIC -- {counts['control_dummy_devices']} control-plane "
            "placeholder device(s) DO NOT EXIST in the vManage data. The "
            "controllers' control traffic really does ride the VPN 0 transport "
            f"drawn above ({counts['control_conn_rows']} control connection(s) "
            f"reported across {counts['edges_with_control_data']} Edge(s)), but "
            "the controllers sit on their OWN subnet, disjoint from every Edge "
            "transport subnet, and vManage exposes no route table to say how "
            "many routed hops lie between them "
            "(/dataservice/device/ip/routetable answers with an empty data "
            "array), so linking a controller straight to a transport cloud "
            "would assert an adjacency the data contradicts. Instead a Router "
            "stands in for the unknown routed hop -- linked DOWN to ONE "
            "transport segment (see the selection caveat below) -- and a "
            f"Switch stands in for the segment the {counts['controllers_linked']} "
            "controller(s) really do share. Both are drawn light gray, this "
            "repo's palette colour for a synthesised placeholder, and their "
            "pseudo ports are numbered from 'Dummy 0' (unlike the transport "
            "clouds, which reserve 'Dummy 0' for their SVI)."
        )
        caveats.append(
            f"MODEL -- {counts['control_l2_segments']} L2 segment entr(y/ies) "
            f"named '{_CONTROL_L2_SEGMENT}' are written onto EVERY port of the "
            "synthetic control-plane switch -- the uplink and every controller "
            "link -- and onto no controller port, the same asymmetry the "
            "transport clouds use, which is what keeps each controller's own "
            "port a routed L3 interface. The two SYNTHETIC hops carry no IP "
            "address: they stand in for structure that was never measured. The "
            "Router carries no L2 segment either, so it reads as a routed hop "
            "rather than a switchport."
        )
        if counts["control_ip_assignments"]:
            caveats.append(
                f"MODEL -- {counts['control_ip_assignments']} controller VPN 0 "
                "interface(s) carry their REPORTED address from "
                "/dataservice/device/interface, so the segment the controllers "
                "share is a real subnet in the L3 diagram rather than an "
                "L1/L2-only construct. NO L3 instance (VRF) is written onto "
                "them: a controller is a Server-role appliance, not a routing "
                "node, and the Transport VPN is an Edge concept -- so they land "
                "in Network Sketcher's default instance, while the Edges' own "
                f"VPN 0 ports stay in '{transport_vrf}'. A controller's VPN 512 "
                "out-of-band management port is still excluded (a genuinely "
                "separate network). Note that this gives the controllers' area "
                "L3 content, so the L3-less-area reordering below no longer "
                "fires for it."
            )
        caveats.append(
            "MODEL -- the synthetic Router links to exactly ONE transport "
            "cloud, the most likely carrier of the control plane: "
            f"{counts.get('control_uplink_chosen', 'n/a')}. It is picked by "
            "weight of evidence -- observed control-connection rows, then "
            "controllers reached, then member Edges, then the cloud name for "
            "a deterministic tie-break -- because the control plane really "
            "does leave the fabric over a single routed path, and linking "
            "every observed cloud turned the Router into a hub touching all "
            "of them. The row's colour is confirmed by ADDRESS via "
            "control/waninterface rather than trusted by name, so a row whose "
            "colour that Edge has no VPN0 port for votes for nothing and is "
            "counted as unresolved. Cloud(s) observed but NOT drawn: "
            f"{counts.get('control_uplink_rejected') or 'none'}. The observed "
            "per-controller detail is preserved as an annotation on each "
            "controller and on the Router itself, and in full here: "
            + "; ".join(control_plane_detail) + "."
        )
        if counts["controllers_history_only"]:
            caveats.append(
                f"MODEL -- {counts['controllers_history_only']} controller(s) "
                "hold no CURRENT control connection and are annotated 'history "
                "only (torn down)' rather than linked over a transport "
                "segment. This is the normal steady state for a vBond "
                "(Validator): its connections are torn down once onboarding "
                "completes, so they survive only in "
                "/dataservice/device/control/connectionshistory, with "
                "state='tear_down' and an unusable system-ip of 0.0.0.0 that "
                "can be joined to the device by its transport address alone. "
                "Such a controller is still drawn on the shared segment, which "
                "is a fact about its subnet, not about a live session."
            )
    else:
        caveats.append(
            "MODEL -- vManage's control-connection endpoints "
            "(/dataservice/device/control/synced/connections, "
            "/dataservice/device/control/connections) returned no usable rows "
            "for any SD-WAN Edge in this export (either the export has no "
            "control_connections data at all -- the case for any export "
            "predating this feature -- or none of it could be joined to a "
            "known controller and to an inferred transport segment). The "
            "vManage / vSmart / vBond controllers are therefore drawn as "
            "unconnected inventory nodes, exactly as before this feature "
            "existed; no colour and no address is ever synthesised to bridge "
            "the gap."
        )
    if counts["control_conn_unresolved"]:
        caveats.append(
            f"MODEL -- {counts['control_conn_unresolved']} control-connection "
            "row(s) were skipped: the session was not 'up', the peer could not "
            "be joined to a known controller (by system-ip, then by "
            "private-ip/public-ip -- which differ on a NATed or cloud-hosted "
            "deployment), or the Edge's local colour resolved to no inferred "
            "transport segment (e.g. that port was consumed by an OBSERVED "
            "LLDP/CDP link in Pass 1, which builds no cloud)."
        )
    caveats.append(
        "OUT OF SCOPE (by design) -- a controller's VPN 512 out-of-band "
        "management port (vManage's 'eth0', typically on a private management "
        "subnet) is NOT drawn: it is a genuinely separate network from the "
        "SD-WAN transport fabric. The control traffic itself is not "
        "out-of-band -- it rides the VPN 0 transport modelled above."
    )
    if not model.l1_links:
        caveats.append(
            "No VPN0 physical interfaces with a resolvable IPv4 address were "
            "found on any SD-WAN Edge; the underlay diagram shows isolated "
            "devices only."
        )

    info = {"mappings": mappings, "counts": counts, "caveats": caveats}
    return model, info


def _order_l3_less_areas_last(model: NSModel) -> int:
    """Move every area with no IP-bearing (L3) interface to the END of its
    ``add area_location`` row, keeping the relative order of the rest.

    Works around a bug in the Network Sketcher engine's all-areas L3 renderer
    (``nsm_l3_diagram_create.calculate_area_offset``): each area's horizontal
    offset is the sum of the widths of the areas PRECEDING it in its row, but
    the width lookup only holds areas that actually carry L3 content, so an
    L3-less area sitting anywhere but last aborts the export with a
    ``KeyError``. A row that is L3-less END TO END is left alone: the renderer
    never looks it up, which is why the waypoint row -- and the controllers'
    row before their VPN 0 addresses were assigned -- was always safe.

    Returns the number of areas moved.
    """
    l3_areas = {
        model.devices[ip.device].area
        for ip in model.ip_assignments
        if ip.device in model.devices
    }
    moved = 0
    for row in model.areas:
        l3_less = [area for area in row if area not in l3_areas]
        kept = [area for area in row if area in l3_areas]
        if not l3_less or not kept:
            continue   # nothing to reorder (the engine never looks the row up)
        row[:] = kept + l3_less
        moved += len(l3_less)
    return moved


def _promote_controller_area_to_top(
    model: NSModel, controller_areas: Set[str],
) -> bool:
    """Lift the controllers' area(s) into a row of their own at the TOP of the
    ``add area_location`` layout.

    ``build_area_layout`` puts every non-waypoint area on a single row BELOW
    the waypoint row, which leaves the controllers beside the sites and their
    control-plane path climbing back UP over the transport clouds. Placing them
    above the clouds instead keeps that path pointing downwards, the way each
    Edge's own transport uplink already does (NS RULE 0), and pairs with the
    reversed internal row order of the construct (see ``_ROW_CONTROL``).

    An area holding an Edge as well -- a controller sharing a site-id with one
    -- is left where the layout helper put it, so lifting the controllers never
    drags a site out of the site row. Runs as a post-pass, like
    ``_order_l3_less_areas_last``, so the shared layout helper stays untouched.

    This controls the ROW an area sits on, which is all ``add area_location``
    can express. The COLUMN is chosen by Network Sketcher from the rendered
    width of each row and cannot be steered from the CLI -- see the README.

    Returns True if the layout changed.
    """
    edge_areas = {d.area for d in model.devices.values() if d.row >= _ROW_EDGE}
    wanted = {
        area for area in controller_areas
        if area in model.area_to_devices and area not in edge_areas
    }
    if not wanted:
        return False
    ordered = [area for row in model.areas for area in row if area in wanted]
    if not ordered:
        return False
    remaining = [
        [area for area in row if area not in wanted] for row in model.areas
    ]
    model.areas = [ordered] + [row for row in remaining if row]
    return True


def _transport_groups_for_color(
    edge_name: str,
    color: str,
    wan_interfaces: List[Dict[str, Any]],
    edge_ports: Dict[str, Dict[str, Dict[str, str]]],
) -> List[str]:
    """The transport COLOUR(s) an Edge reaches a given control-plane colour
    over, resolved BY ADDRESS rather than trusted from the row.

    ``control/waninterface`` maps the row's colour to the Edge's own transport
    address(es), which are then matched against its already-resolved VPN0
    physical ports. Going through the addresses keeps a row whose colour this
    Edge has no VPN0 port for from voting for a cloud it does not sit on --
    such a row is counted as unresolved instead.

    The returned key is the colour alone, matching how the transport clouds
    themselves are now grouped: one cloud per colour, subnet-independent.
    """
    color = (color or "").strip()
    if not color:
        return []
    addresses: set = set()
    for wan in wan_interfaces or []:
        if str(wan.get("color") or "").strip() != color:
            continue
        for key in ("private-ip", "public-ip"):
            value = str(wan.get(key) or "").strip()
            if value and value != "0.0.0.0":
                addresses.add(value)
    out: List[str] = []
    for _port, info in sorted(edge_ports.get(edge_name, {}).items()):
        if info["ip"] not in addresses:
            continue
        if info["color"] not in out:
            out.append(info["color"])
    return out


def _apply_control_plane_links(
    idx,
    model: NSModel,
    mappings: List[StencilMapping],
    sysip_to_name: Dict[str, str],
    edge_ports: Dict[str, Dict[str, Dict[str, str]]],
    seg_by_group: Dict[str, str],
    cloud_next_port: Dict[str, int],
    unique_name: Callable[[str], str],
    counts: Dict[str, Any],
    l3_name: str,
    l2_name: str,
) -> List[str]:
    """Draw the controllers' control-plane path down from the transport.

    vManage reports, per Edge, every control connection that Edge holds UP to
    a controller (``idx.control_connections``): the controller's identity, the
    Edge's own LOCAL transport colour, and the controller's transport address.
    That is enough to say WHICH inferred transport segment carries the control
    plane, but not how many routed hops lie between it and the controllers --
    the controllers sit on their own subnet, disjoint from every Edge
    transport subnet, and vManage exposes no route table to measure the
    distance. Linking a controller straight to a transport cloud would
    therefore assert an adjacency the data contradicts.

    So the path is drawn with two synthetic hops instead, stacked BELOW the
    controllers (their area sits above the transport clouds, see
    :func:`_promote_controller_area_to_top`): a Switch (``Dummy_l2``) standing
    in for the segment the controllers really do share, and under it a Router
    (``Dummy_l3``) standing in for the unknown routed hop, linked DOWN to ONE
    transport segment -- the single most likely carrier of the control plane,
    picked by weight of evidence: observed row count, then the number of
    controllers reached, then the segment's member count, then its name. Every
    controller links to the switch, which carries one L2 segment on ALL of its
    ports so they land in one broadcast domain. Ports on both devices are
    pseudo ports numbered from ``Dummy 0``.

    Each controller's OWN VPN 0 interface keeps its reported address -- but no
    L3 instance (VRF): a controller is a Server-role appliance, not a routing
    node, and the Transport VPN is an Edge concept. That is enough to make the
    controllers' shared segment a real subnet in the L3 diagram. The two
    synthetic hops get no IP at all: they stand in for structure that was never
    measured. The L2 segment
    lives on the switch's ports only -- the same asymmetry the transport clouds
    use -- so each controller's own port stays a routed L3 interface.

    A row that is down, names an unknown peer, or resolves to no inferred
    segment is counted and skipped, never fabricated; when nothing resolves at
    all the controllers are simply left as unconnected inventory nodes.

    Returns one human-readable line per controller describing how its OWN
    control plane was observed -- the per-controller nuance the shared
    ``Dummy_l3`` uplinks flatten out (a vManage reachable over one colour only
    still shares that router with the vSmarts) -- for the caller's caveats.
    """
    controller_name: Dict[str, str] = {}    # controller system-ip -> NS name
    controller_port: Dict[str, str] = {}    # NS name -> its real VPN0 port
    controller_cidr: Dict[str, str] = {}    # NS name -> that port's a.b.c.d/nn
    controller_by_ip: Dict[str, str] = {}   # transport address -> NS name
    for dev in idx.devices:
        if topo.device_personality(dev) not in topo.CONTROLLER_PERSONALITIES:
            continue
        sysip = str(dev.get("system-ip") or "")
        name = sysip_to_name.get(sysip)
        if not name or name not in model.devices:
            continue
        controller_name[sysip] = name
        for itf in topo.controller_vpn0_interfaces(idx.interfaces.get(sysip, [])):
            port = normalise_port_name(str(itf.get("ifname") or itf.get("interface") or ""))
            if port and name not in controller_port:
                # The port that will carry the L1 link -- and, since it is a
                # real interface with a reported address, the IP too.
                controller_port[name] = port
                cidr = topo.interface_cidr(itf)
                if cidr:
                    controller_cidr[name] = cidr
            ip = topo.interface_ip(itf)
            if ip:
                controller_by_ip.setdefault(ip, name)
    if not controller_name:
        return []

    def _resolve_peer(row: Dict[str, Any]) -> str:
        """The controller a control-connection row points at, by system-ip
        first and by transport address second -- the latter being the only
        join available for a torn-down row, which reports ``0.0.0.0``."""
        sysip = topo.control_connection_peer_sysip(row)
        if sysip in controller_name:
            return controller_name[sysip]
        for address in topo.control_connection_peer_addresses(row):
            if address in controller_by_ip:
                return controller_by_ip[address]
        return ""

    reached: Dict[str, Set[str]] = defaultdict(set)
    # Weight of evidence per transport colour, for picking the ONE uplink below.
    group_rows: Dict[str, int] = defaultdict(int)
    group_controllers: Dict[str, Set[str]] = defaultdict(set)
    connections = getattr(idx, "control_connections", {}) or {}
    for dev in idx.devices:
        if topo.device_personality(dev) != "vedge":
            continue
        sysip = str(dev.get("system-ip") or "")
        name = sysip_to_name.get(sysip)
        rows = connections.get(sysip) or []
        if not name or not rows:
            continue
        counts["edges_with_control_data"] += 1
        wan_interfaces = idx.wan_interfaces.get(sysip, [])
        for row in rows:
            counts["control_conn_rows"] += 1
            peer = _resolve_peer(row) if topo.control_connection_is_up(row) else ""
            groups = [
                group for group in _transport_groups_for_color(
                    name, topo.control_connection_local_color(row),
                    wan_interfaces, edge_ports,
                )
                if group in seg_by_group
            ]
            if not peer or not groups:
                counts["control_conn_unresolved"] += 1
                continue
            reached[peer].update(groups)
            for group in groups:
                group_rows[group] += 1
                group_controllers[group].add(peer)

    # Member Edge count per transport colour -- the third tie-break below: a
    # cloud more Edges sit on is the likelier path off the fabric.
    group_members: Dict[str, int] = defaultdict(int)
    for ports in edge_ports.values():
        for group in {info["color"] for info in ports.values()}:
            group_members[group] += 1

    def _uplink_rank(group: str) -> Tuple[int, int, int, str]:
        """Sort key for 'most likely carrier of the control plane', strongest
        first: observed rows, controllers reached, member Edges, then the
        cloud name for a deterministic tie-break."""
        return (-group_rows[group], -len(group_controllers[group]),
                -group_members[group], seg_by_group[group])

    # A vBond tears its control connections down once onboarding completes, so
    # it appears in the history endpoint only -- recorded as an annotation, not
    # as a link: a torn-down session is not current connectivity.
    history_only: Set[str] = set()
    for rows in (getattr(idx, "control_connections_history", {}) or {}).values():
        for row in rows or []:
            peer = _resolve_peer(row)
            if peer and peer not in reached:
                history_only.add(peer)
    counts["controllers_history_only"] = len(history_only)

    controllers_by_area: Dict[str, List[str]] = defaultdict(list)
    for name in sorted(set(controller_name.values())):
        controllers_by_area[model.devices[name].area].append(name)

    def _reach_detail(name: str, with_network: bool = False) -> str:
        """How this controller's own control plane was observed. The compact
        form goes into the device attributes, which the command builder
        truncates; the caller's caveat gets the sentence-shaped form."""
        if reached.get(name):
            if with_network:
                return "over " + ", ".join(sorted(reached[name]))
            return "+".join(sorted(reached[name]))
        if name in history_only:
            return "history only (torn down)" if with_network else "history only"
        return "no control connection reported" if with_network else "none reported"

    details = [
        f"{name} {_reach_detail(name, with_network=True)}"
        for name in sorted(set(controller_name.values()))
    ]

    for area in sorted(controllers_by_area):
        controllers = controllers_by_area[area]
        observed = sorted(
            {group for name in controllers for group in reached.get(name, set())},
            key=_uplink_rank,
        )
        if not observed:
            continue
        # ONE uplink only: the control plane leaves this fabric over a single
        # routed path in reality, and drawing every segment it was observed
        # over turned the synthetic Router into a hub touching all of them.
        # The runners-up are not lost -- they go into the caller's caveats.
        uplinks, runners_up = observed[:1], observed[1:]
        counts["control_uplink_chosen"] = (
            f"{seg_by_group[uplinks[0]]} ({group_rows[uplinks[0]]} row(s), "
            f"{len(group_controllers[uplinks[0]])} controller(s), "
            f"{group_members[uplinks[0]]} member Edge(s))"
        )
        counts["control_uplink_rejected"] = "; ".join(
            f"{seg_by_group[g]} ({group_rows[g]} row(s), "
            f"{len(group_controllers[g])} controller(s), "
            f"{group_members[g]} member Edge(s))" for g in runners_up
        )

        l3_dev = unique_name(l3_name)
        l3_st = sm.map_logical(
            l3_dev, "control-l3-dummy",
            model="Synthetic routed hop between the transport network(s) "
                  "and the SD-WAN controllers",
        )
        mappings.append(l3_st)
        model.devices[l3_dev] = NSDevice(
            name=l3_dev, area=area, row=_ROW_CONTROL_L3, stencil=l3_st,
            is_endpoint=False, default_color=_CONTROL_DUMMY_COLOR,
            port_info=_LAN_DUMMY_PORT_INFO,
            routing_attribute=(
                "synthetic routed hop, hop count unknown | " + " | ".join(
                    f"{name}: {_reach_detail(name)}" for name in controllers)
            ),
        )
        l2_dev = unique_name(l2_name)
        l2_st = sm.map_logical(
            l2_dev, "control-l2-dummy",
            model="Synthetic shared segment carrying the SD-WAN controllers",
        )
        mappings.append(l2_st)
        model.devices[l2_dev] = NSDevice(
            name=l2_dev, area=area, row=_ROW_CONTROL_L2, stencil=l2_st,
            is_endpoint=False, default_color=_CONTROL_DUMMY_COLOR,
            port_info=_LAN_DUMMY_PORT_INFO,
            # No quotes around the segment name: NS parses each attribute cell
            # as a single-quoted Python literal, so a nested "'" breaks it.
            routing_attribute=(
                f"synthetic control-plane segment {_CONTROL_L2_SEGMENT} | "
                f"members: {', '.join(controllers)}"
            ),
        )
        counts["control_dummy_devices"] += 2

        # UP to every transport segment the control plane was observed over.
        # The cloud-side port joins that cloud's own colour L2 segment, like
        # every other link port on it; the Dummy_l3 side carries no segment,
        # which is what makes it read as a router rather than a switchport.
        port_index = 0
        for group in uplinks:
            seg_name = seg_by_group[group]
            cloud_port = f"Dummy {cloud_next_port.get(seg_name, 1)}"
            cloud_next_port[seg_name] = cloud_next_port.get(seg_name, 1) + 1
            model.l1_links.append(
                NSL1Link(l3_dev, f"Dummy {port_index}", seg_name, cloud_port))
            model.l2_segments_phys.append(
                NSL2Segment(device=seg_name, port=cloud_port, vlans=[group]))
            counts["l2_segments"] += 1
            counts["control_uplinks"] += 1
            counts["control_links"] += 1
            port_index += 1

        # DOWN to the shared segment, and from it to every controller. The
        # segment is on the switch's ports ONLY -- the same asymmetry the
        # transport clouds use, so each controller's own port stays a routed
        # L3 interface.
        model.l1_links.append(
            NSL1Link(l2_dev, "Dummy 0", l3_dev, f"Dummy {port_index}"))
        model.l2_segments_phys.append(
            NSL2Segment(device=l2_dev, port="Dummy 0", vlans=[_CONTROL_L2_SEGMENT]))
        counts["control_links"] += 1
        counts["control_l2_segments"] += 1
        for i, name in enumerate(controllers, start=1):
            switch_port = f"Dummy {i}"
            own_port = controller_port.get(name)
            model.l1_links.append(NSL1Link(
                name, own_port or switch_port, l2_dev, switch_port))
            model.l2_segments_phys.append(
                NSL2Segment(device=l2_dev, port=switch_port, vlans=[_CONTROL_L2_SEGMENT]))
            counts["control_links"] += 1
            counts["control_l2_segments"] += 1
            counts["controllers_linked"] += 1
            # The controller's own VPN 0 port is a real interface with a real
            # reported address, so it carries that address -- but NO VRF: a
            # controller is a Server-role appliance, not a routing node, and
            # the Transport VPN is an Edge concept. Its VPN 512 out-of-band
            # management port is excluded upstream by controller_vpn0_interfaces.
            cidr = controller_cidr.get(name) if own_port else ""
            if cidr:
                model.ip_assignments.append(
                    NSIPAssignment(device=name, port=own_port, cidrs=[cidr]))
                counts["control_ip_assignments"] += 1
                counts["ip_assignments"] += 1
            device = model.devices[name]
            device.routing_attribute = " | ".join(
                bit for bit in (device.routing_attribute,
                                f"control plane: {_reach_detail(name)}") if bit)
    return details


def _apply_observed_links(
    idx,
    model: NSModel,
    sysip_to_name: Dict[str, str],
    host_by_lower: Dict[str, str],
    edge_ports: Dict[str, Dict[str, Dict[str, str]]],
    used_ports: set,
    counts: Dict[str, Any],
    prefer_lldp: bool = True,
) -> None:
    """PASS 1 (preferred, OBSERVED): resolve every SD-WAN Edge's LLDP/CDP
    neighbor entries into real device-to-device L1 links.

    Restricted to entries whose LOCAL interface is one of that Edge's known
    VPN0 PHYSICAL (WAN) ports (``edge_ports``) -- matching this mapper's
    scope (VPN0 transport plumbing only). The neighbor's reported identity is
    joined to another already-known SD-WAN-managed device via
    ``sdwan_topology.resolve_neighbor_device``; entries that do not resolve
    (a real but unmanaged/non-SD-WAN neighbour, or a schema mismatch -- see
    the module docstring) are skipped, not fabricated.

    One cable per physical port (mirrors every other converter's convention)
    -- ``used_ports`` is populated as this pass claims each end, so the
    caller's Pass 2 per-colour inference never re-uses a port already
    given a real observed link here. When BOTH protocols report the SAME
    local port, LLDP wins by default (``prefer_lldp``) since it is the
    universal protocol; CDP is IOS-XE/cEdge-only and is treated purely as a
    secondary confirmation source, never double-counted into a second link.
    """
    link_seen: set = set()  # frozenset({(devA,portA),(devB,portB)}) already drawn

    edges_with_data: set = set()
    for sysip, entries in idx.lldp_neighbors.items():
        if entries and sysip in sysip_to_name:
            edges_with_data.add(sysip)
    for sysip, entries in idx.cdp_neighbors.items():
        if entries and sysip in sysip_to_name:
            edges_with_data.add(sysip)
    counts["edges_with_neighbor_data"] = len(edges_with_data)

    def _apply_protocol(neighbor_map: Dict[str, List[Dict[str, Any]]], kind: str) -> None:
        for dev in idx.devices:
            if topo.device_personality(dev) != "vedge":
                continue
            sysip = str(dev.get("system-ip") or "")
            name = sysip_to_name.get(sysip)
            if not name:
                continue
            entries = neighbor_map.get(sysip) or []
            if not entries:
                continue
            local_ports = edge_ports.get(name, {})
            for entry in entries:
                local_port = normalise_port_name(topo.neighbor_local_port(entry))
                if not local_port or local_port not in local_ports:
                    continue  # out of scope: not one of this Edge's VPN0 physical ports
                if (name, local_port) in used_ports:
                    continue  # already claimed (LLDP already resolved it, if this is CDP)
                remote_name = topo.resolve_neighbor_device(entry, sysip_to_name, host_by_lower)
                if not remote_name or remote_name == name or remote_name not in model.devices:
                    counts["unresolved_neighbors"] += 1
                    continue
                remote_port = normalise_port_name(topo.neighbor_remote_port(entry)) \
                    or "GigabitEthernet 0/0"
                if (remote_name, remote_port) in used_ports:
                    continue  # remote port already cabled by another entry
                key = frozenset({(name, local_port), (remote_name, remote_port)})
                if key in link_seen:
                    continue
                link_seen.add(key)
                used_ports.add((name, local_port))
                used_ports.add((remote_name, remote_port))
                model.l1_links.append(NSL1Link(name, local_port, remote_name, remote_port))
                info = local_ports[local_port]
                model.ip_assignments.append(
                    NSIPAssignment(device=name, port=local_port, cidrs=[info["cidr"]]))
                counts["ip_assignments"] += 1
                remote_info = edge_ports.get(remote_name, {}).get(remote_port)
                if remote_info:
                    model.ip_assignments.append(
                        NSIPAssignment(device=remote_name, port=remote_port,
                                       cidrs=[remote_info["cidr"]]))
                    counts["ip_assignments"] += 1
                counts["observed_links"] += 1
                counts[f"observed_links_{kind}"] += 1

    protocols = [(idx.lldp_neighbors, "lldp"), (idx.cdp_neighbors, "cdp")]
    if not prefer_lldp:
        protocols.reverse()
    for neighbor_map, kind in protocols:
        _apply_protocol(neighbor_map, kind)
