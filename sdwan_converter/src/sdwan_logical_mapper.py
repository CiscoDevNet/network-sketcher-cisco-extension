# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""Build the *Service VPN overlay* NSModel from a Cisco Catalyst SD-WAN
(vManage) export -- the VRF/LAN side of the fabric (VPN 10/11/12, ...) that
OMP actually advertises reachability for.

Each Edge is drawn with its WAN side UP and its LAN side DOWN, so the diagram
reads the way an SD-WAN site actually does::

    overlay_wp_   [ Corporate ]   [ PCI ]   [ Guest ]      <- one cloud per VPN
                       |             |         |            (L2 segment here ONLY)
    br1           'Vpn 10'      'Vpn 11'   'Vpn 12'        <- SYNTHETIC WAN ports
                        \\           |          /              (VRF, no IP)
                          [    br1-edge1    ]               row 0
                        /           |          \\
                  'Gi 3'         'Gi 4'      'Gi 5'        <- REAL LAN ports
                        \\           |          /              (real IP + VRF)
                          [     Dummy_1     ]               row 4, SYNTHETIC

Scope (by design):
  * Devices: SD-WAN Edge (cEdge/vEdge) ONLY, and only those that actually
    have at least one Service VPN interface -- an Edge with none is left out
    entirely rather than drawn as an isolated node. Controllers
    (vManage/vSmart/vBond) are orchestration/control-plane and are NOT drawn.
    Each Edge sits in the NS area named after its site (see
    ``sdwan_topology.build_site_area_map``: ``site-name`` -> ``br1``, falling
    back to ``site<id>``).
  * Waypoints: one Cloud device PER Service VPN id, in a shared ``overlay``
    waypoint area (RULE 3: a device in a non-waypoint site area may link to a
    waypoint area; multiple site areas may each link to the SAME waypoint).
    Its label is the VPN's configured NAME (``Corporate``) when the export
    carries ``vpn_names``, else ``VPN <id>``.
  * WAN side -- one SYNTHETIC port ``Vpn <id>`` per Service VPN the Edge
    participates in, L1-linked UP to that VPN's cloud on a ``Dummy N``
    pseudo-port. It carries the VRF but NO IP: it stands for the Edge's
    membership of the VPN, not for any real interface (the real overlay
    transport is the IPsec/OMP fabric in VPN 0, drawn in the underlay). The
    VPN label is carried as an L2 segment on the CLOUD side of that link
    ONLY -- never on the Edge's port. Network Sketcher treats any port
    carrying an L2 segment as an L2 switchport and drops it from the L3
    interface table, so segmenting the Edge port would make its VRF
    unassignable ("No matching entry found ..."). The segment exists purely
    to group the cloud's internal ``Dummy N`` ports into one broadcast domain
    per VPN in the L2 diagram.
  * LAN side -- every REAL physical Service VPN port (``GigabitEthernet 3``,
    ...) is L1-linked DOWN to ``Dummy_<n>``, a SYNTHETIC per-Edge LAN
    aggregation switch (one per Edge, ``_ROW_LAN_DUMMY``). This whole side --
    the switches and their numbering, the links, the IPs, the Loopbacks and
    every VRF label -- is planned and drawn by ``sdwan_lan_side``, shared
    verbatim with the UNDERLAY so both diagrams put the same switches under
    the same Edges. The Edge port keeps
    its reported IPv4 address and gets its VPN's VRF; the Dummy-side port gets
    the SAME VRF and no IP, so the synthetic switch is itself split by VRF
    rather than pretending to be one flat LAN. Neither side carries an L2
    segment -- same NS constraint as above.
  * Loopback in a Service VPN: an UNCONNECTED virtual port + its IP + the VRF
    rename -- no L1 link and no L2 segment. NS RULE 15 names Loopback as an
    explicit exception to SVI/L2-segment binding, and every other converter
    in this repo models Loopbacks the same way.
  * Explicitly OUT OF SCOPE: VPN 0 (the Transport VPN -- that is the
    UNDERLAY's subject, see ``sdwan_physical_mapper``), Tunnel interfaces and
    transport colours, the IPsec+BFD Edge-to-Edge tunnel mesh, and
    OMP-advertised routes.

Every waypoint cloud carries ONE extra ``Dummy 0`` port modelled as an SVI and
self-bound to that cloud's own VPN L2 segment (NS RULE 15). It has no L1 link
and no IP -- it only gives the segment an L3/SVI anchor on the cloud.

The Edges and the Service VPN waypoint clouds are participants in the logical
Service VPN topology and are coloured light purple (``OVERLAY_COLOR``), the
same convention ``aci_converter`` / ``catc_converter`` use for their overlay
diagrams. The synthetic per-Edge LAN switches are light gray
(``LAN_DUMMY_COLOR``) instead -- this repo's shared palette reserves that
colour for synthesised placeholders with no counterpart in the source data.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from . import sdwan_lan_side as lan_side
from . import sdwan_stencil_mapper as sm
from . import sdwan_topology as topo
from .sdwan_stencil_mapper import StencilMapping
from .ns_model import (
    NSDevice, NSL1Link, NSL2Segment, NSModel, NSVirtualPort, build_area_layout,
)

# Every overlay device (Edge + Service VPN waypoint cloud) is coloured light
# purple -- the same convention aci_converter / catc_converter use for their
# logical/overlay diagrams.
OVERLAY_COLOR = (221, 204, 255)

# The synthetic per-Edge LAN switch deviates from OVERLAY_COLOR: it is not a
# participant in the fabric at all but a pure placeholder with no vManage
# counterpart, and this repo's shared palette reserves light gray for exactly
# that (the same value ns_command_builder uses as _COLOR_WAYPOINT).
LAN_DUMMY_COLOR = (200, 200, 200)

# One extra port per waypoint cloud, modelled as an SVI and self-bound to that
# cloud's own VPN L2 segment (NS RULE 15). Synthesised: no vManage
# counterpart, no L1 link and no IP. Safe against the cloud's L1 'Dummy N'
# ports, which are numbered from 1 (see the WAN-side loop below).
_CLOUD_SVI_PORT = "Dummy 0"

_ROW_EDGE = 0
_ROW_LAN_DUMMY = 4          # RULE 0 Access tier -- drawn BELOW its Edge
_AREA_OVERLAY = "overlay"   # raw waypoint area name (promoted to 'overlay_wp_')

_DEFAULT_FALLBACK_FMT = "VPN {id}"


def _dummy(n: int) -> str:
    """Pseudo port on a synthetic device (waypoint cloud / LAN dummy) -- the
    repo-wide convention for a port with no real interface behind it."""
    return f"Dummy {n}"


def _vpn_port(vpn_id: int) -> str:
    """SYNTHETIC WAN-side port name for a Service VPN (``Vpn 10``).

    Deliberately NOT run through ``normalise_port_name``: ``Vpn`` is not one
    of the Cisco interface-type tokens that function canonicalises, so the
    name is built here in its final form and passed through verbatim.
    """
    return f"Vpn {vpn_id}"


def build_logical_model(
    idx,
    cfg: Optional[Dict[str, Any]] = None,
    layout: str = "tier",
) -> Tuple[NSModel, Dict[str, Any]]:
    """Return (NSModel, info) for the SD-WAN Service VPN overlay."""
    cfg = cfg or {}
    fallback_fmt = str(cfg.get("vpn_name_fallback_format") or _DEFAULT_FALLBACK_FMT)

    # The LAN side -- which Edges have Service VPN interfaces at all, their
    # physical ports and Loopbacks, every VRF label, and the name of each
    # per-Edge 'Dummy_<n>' switch -- is planned by the shared helper the
    # UNDERLAY calls too, so the two diagrams cannot drift apart.
    plan = lan_side.plan_service_vpn_lan_side(idx, cfg)
    vpn_names = plan.vpn_names
    excluded = plan.excluded

    model = NSModel()
    mappings: List[StencilMapping] = []
    seen_names: set = set()
    counts = {
        "edge": 0, "service_vpn_waypoint": 0, "lan_dummy": 0, "l1_links": 0,
        "vpn_ports": 0, "lan_links": 0,
        "ip_assignments": 0, "l2_segments": 0, "virtual_ports": 0,
        "cloud_svi_ports": 0, "unresolved_vpn_name": 0,
    }
    caveats: List[str] = []

    def _unique(name: str) -> str:
        name = topo.sanitize(name) or "device"
        base, i = name, 2
        while name in seen_names:
            name = f"{base}_{i}"
            i += 1
        seen_names.add(name)
        return name

    # An Edge with no Service VPN interface at all is absent from the plan and
    # is therefore simply never drawn here (no isolated nodes).
    skipped_edges = plan.skipped_edges

    # ----- one purple Cloud waypoint per Service VPN ----------------------
    # Created before the Edges so the clouds keep a stable, VPN-id-ordered
    # naming sequence regardless of which Edge is processed first.
    vpn_cloud: Dict[int, str] = {}
    vpn_label: Dict[int, str] = {}
    for vpn_id in sorted(plan.vpn_ids):
        label = topo.resolve_vpn_label(vpn_id, vpn_names, fallback_fmt)
        if str(vpn_id) not in vpn_names:
            counts["unresolved_vpn_name"] += 1
        vpn_label[vpn_id] = label
        cloud_name = _unique(label)
        st = sm.map_logical(
            cloud_name, "service-vpn-waypoint",
            model=f"SD-WAN Service VPN waypoint (vpn-id={vpn_id})",
        )
        mappings.append(st)
        model.devices[cloud_name] = NSDevice(
            name=cloud_name, area=_AREA_OVERLAY, row=0, stencil=st, is_endpoint=False,
            default_color=OVERLAY_COLOR,
            routing_attribute=f"vpn-id={vpn_id} | name={label}",
        )
        vpn_cloud[vpn_id] = cloud_name
        counts["service_vpn_waypoint"] += 1

        # RULE 15 SVI self-binding -- the cloud's 'Dummy 0' port is bound to
        # the very same segment name its 'Dummy N' link ports carry below, so
        # the two can never drift apart. No L1 link and no IP: the SVI only
        # gives the VPN segment an L3 anchor on the cloud.
        model.virtual_ports.append(
            NSVirtualPort(device=cloud_name, port=_CLOUD_SVI_PORT))
        model.l2_segments_svi.append(
            NSL2Segment(device=cloud_name, port=_CLOUD_SVI_PORT,
                        vlans=[vpn_label[vpn_id]]))
        counts["cloud_svi_ports"] += 1

    # ----- Edge devices + their Service VPN interfaces --------------------
    # Each Edge is split into a WAN side (synthetic 'Vpn <id>' ports linking
    # UP to the VPN clouds) and a LAN side (its REAL physical ports linking
    # DOWN to one synthetic per-Edge 'Dummy_<n>' switch).
    cloud_port: Dict[str, int] = defaultdict(int)
    loopback_count = 0
    site_area = topo.build_site_area_map(idx.devices)
    for lan in plan.edges:
        dev = lan.device
        name = _unique(lan.display_name)
        st = sm.map_device(
            name=name, personality="vedge",
            device_model=dev.get("device-model") or "",
            uuid=dev.get("uuid") or "",
            inferred=topo.device_is_inferred_personality(dev),
        )
        mappings.append(st)

        sysip = str(dev.get("system-ip") or "")
        site_id = dev.get("site-id")
        area = (site_area.get(str(site_id).strip(), f"site{site_id}")
                if site_id not in (None, "") else "default")
        attr_bits: List[str] = []
        if sysip:
            attr_bits.append(f"system-ip {sysip}")
        if site_id not in (None, ""):
            attr_bits.append(f"site-id {site_id}")
        attr_bits.append(
            "service VPNs " + ",".join(str(v) for v in lan.service_vpn_ids)
        )
        model.devices[name] = NSDevice(
            name=name, area=area, row=_ROW_EDGE, stencil=st, is_endpoint=False,
            default_color=OVERLAY_COLOR,
            routing_attribute=" | ".join(attr_bits),
        )
        counts["edge"] += 1

        # ----- WAN side: one synthetic 'Vpn <id>' port per Service VPN -----
        # No IP: this port stands for the Edge's MEMBERSHIP of the VPN, not
        # for any real interface. The L2 segment goes on the CLOUD side only
        # (NS drops an L2-segmented port from the L3 interface table, which
        # would make the VRF rename below fail). This side is the OVERLAY's
        # alone -- the underlay draws the VPN0 transport clouds instead.
        for vpn_id in sorted(lan.port_vpn_ids):
            wan_port = _vpn_port(vpn_id)
            cloud = vpn_cloud[vpn_id]
            cloud_port[cloud] += 1
            cport = _dummy(cloud_port[cloud])
            model.l1_links.append(NSL1Link(name, wan_port, cloud, cport))
            model.l2_segments_phys.append(
                NSL2Segment(device=cloud, port=cport, vlans=[vpn_label[vpn_id]]))
            model.vrf_renames.append((name, wan_port, lan.vrf_of[vpn_id]))
            counts["l1_links"] += 1
            counts["l2_segments"] += 1
            counts["vpn_ports"] += 1

        # ----- LAN side + Loopbacks: shared verbatim with the underlay -----
        lan_counts = lan_side.apply_edge_lan_side(
            model, mappings, name, area, lan,
            unique_name=_unique, row=_ROW_LAN_DUMMY, color=LAN_DUMMY_COLOR,
        )
        counts["lan_dummy"] += lan_counts.lan_dummy
        counts["l1_links"] += lan_counts.lan_links
        counts["lan_links"] += lan_counts.lan_links
        counts["ip_assignments"] += lan_counts.ip_assignments
        counts["virtual_ports"] += lan_counts.loopback_ports
        loopback_count += lan_counts.loopback_ports

    model.areas, model.area_to_devices = build_area_layout(
        model.devices, model.l1_links, layout=layout,
    )

    # ----- caveats ----------------------------------------------------------
    caveats.append(
        "MODEL -- this overlay draws the SD-WAN SERVICE VPN (VRF/LAN) "
        "topology with each Edge split into a WAN side and a LAN side: ABOVE "
        "the Edge, one synthetic 'Vpn <id>' port per Service VPN links up to "
        "that VPN's purple waypoint cloud (a star, using the pseudo port "
        "'Dummy N' on the cloud side); BELOW it, every REAL physical Service "
        "VPN interface links down to that Edge's own synthetic 'Dummy_<n>' "
        "LAN switch. VPN 0 (the Transport VPN), Tunnel interfaces, transport "
        "colours and the IPsec+BFD Edge-to-Edge tunnel mesh are NOT drawn "
        "here -- see sdwan_physical_mapper for the VPN0 transport underlay."
    )
    caveats.append(
        "SYNTHETIC -- the 'Vpn <id>' ports and the 'Dummy_<n>' LAN switches "
        "DO NOT EXIST in the vManage data; they are drawing devices invented "
        "by this converter. A 'Vpn <id>' port is not an interface: it stands "
        "for the Edge's MEMBERSHIP of that Service VPN, which is why it "
        "carries a VRF but no IP address. The real overlay data path is the "
        "SD-WAN IPsec/OMP fabric riding VPN 0, which is drawn in the UNDERLAY "
        "diagram -- not here. A 'Dummy_<n>' switch is likewise not a real "
        "device: it is one synthetic aggregation point per Edge that lets the "
        "Edge's real LAN ports terminate somewhere below it (and be separated "
        "by VRF) instead of floating unconnected. Whatever LAN switching "
        "actually exists behind those ports is not visible to vManage."
    )
    caveats.append(
        "MODEL -- one vpn-id = ONE waypoint cloud fabric-wide. Each site "
        "normally has its OWN subnet inside a given Service VPN, so drawing "
        "them all against one cloud is an ABSTRACTION: the L2 diagram shows "
        "a single broadcast domain per VPN, while the L3 diagram still "
        "separates the real per-site subnets correctly."
    )
    caveats.append(
        "MODEL -- the per-link L2 segment is ASYMMETRIC: it is placed on the "
        "waypoint cloud's 'Dummy N' port only, and on NO port of the Edge or "
        "of its LAN switch. Network Sketcher treats any port carrying an L2 "
        "segment as an L2 switchport and removes it from the L3 interface "
        "table, so segmenting the Edge's 'Vpn <id>' port, its physical LAN "
        "port, or the LAN switch's own port would make that port's IP address "
        "and VRF (l3_instance) unassignable. Every port on the Edge and on "
        "its LAN switch therefore stays a routed L3 port -- which is what the "
        "Edge's Service VPN interfaces really are -- and the cloud-side "
        "segment alone groups each VPN into one broadcast domain in the L2 "
        "diagram."
    )
    caveats.append(
        "MODEL -- the L3 instance (VRF) name is '<VPN name>_vpn<id>' "
        "('Corporate_vpn10'), or plain 'vpn<id>' when the VPN has no "
        "configured name. It is written onto BOTH ends of every LAN link "
        "(the Edge's physical port and the facing LAN-switch port) and onto "
        "the Edge's 'Vpn <id>' and Loopback ports, so the L3 diagram is "
        "separated by VRF exactly as the real fabric is. Only the Edge's own "
        "real interfaces carry an IP address; the synthetic 'Vpn <id>' and "
        "LAN-switch ports carry the VRF alone."
    )
    if counts["cloud_svi_ports"]:
        caveats.append(
            f"SYNTHETIC -- {counts['cloud_svi_ports']} '{_CLOUD_SVI_PORT}' "
            "SVI port(s), one per Service VPN waypoint cloud, are created and "
            "self-bound (Network Sketcher RULE 15) to that cloud's OWN VPN L2 "
            "segment. Each has NO vManage counterpart, NO L1 link and NO IP "
            "address: it exists only to give the VPN segment an L3/SVI anchor "
            "on the cloud. It cannot collide with the cloud's L1 link ports, "
            "which are numbered 'Dummy 1' upwards."
        )
    caveats.append(
        "MODEL -- L2 segment names are the VPN NAME ('Corporate'), NOT the "
        "'Vlan<id>' convention the other converters in this repo use. There "
        "is no VLAN in an SD-WAN Service VPN, and Network Sketcher renders a "
        "non-'Vlan'-prefixed segment name on a WayPoint's Dummy port "
        "correctly (verified)."
    )
    if loopback_count:
        caveats.append(
            f"MODEL -- {loopback_count} Service VPN Loopback interface(s) are "
            "drawn as UNCONNECTED virtual ports (IP + VRF only, no L1 link "
            "and no L2 segment), per Network Sketcher RULE 15, which names "
            "Loopback as an explicit exception to SVI/L2-segment binding. "
            "They appear on the L3 diagram only."
        )
    if counts["lan_dummy"]:
        caveats.append(
            f"SYNTHETIC -- {counts['lan_dummy']} per-Edge LAN switch(es) "
            f"carrying {counts['lan_links']} LAN link(s) were created (one per "
            "Edge with at least one Service VPN PHYSICAL interface, drawn in "
            "the SAME site area as its Edge, on the Access tier row below it). "
            "An Edge whose only Service VPN interfaces are Loopbacks gets no "
            "LAN switch, since there would be nothing to plug into it. They "
            "are coloured light gray rather than the overlay's light purple, "
            "this repo's shared palette colour for a synthesised placeholder "
            "with no counterpart in the source data."
        )
    if counts["ip_assignments"]:
        caveats.append(
            f"IP ADDRESSES -- {counts['ip_assignments']} Service VPN "
            "interface(s) have their reported IPv4 address/mask drawn on this "
            "L3 diagram, each with its VPN's VRF as the port's L3 instance. A "
            "/32 is used only when the export's mask is missing or "
            "unparseable. No IP is ever assigned to a waypoint cloud, to a "
            "synthetic 'Vpn <id>' port or to a LAN switch port (none of them "
            "is a real Layer-3 endpoint)."
        )
    if counts["unresolved_vpn_name"]:
        caveats.append(
            f"INFERRED -- {counts['unresolved_vpn_name']} Service VPN(s) "
            "could not be resolved to a configured VPN name and are labelled "
            f"'{fallback_fmt}' instead. VPN names come from vManage's FEATURE "
            "TEMPLATES, not from device running-config, so they are "
            "unavailable for untemplated or Config-Group-managed devices "
            "(and for any export predating this feature)."
        )
    if excluded:
        caveats.append(
            "MODEL -- Service VPN(s) "
            f"{', '.join(str(v) for v in sorted(excluded))} were excluded by "
            "the 'excluded_service_vpns' configuration value. VPN 0 (the "
            "Transport VPN) and the reserved ids 512 / 65528-65530 are always "
            "excluded regardless of configuration."
        )
    if skipped_edges > 0:
        caveats.append(
            f"MODEL -- {skipped_edges} SD-WAN Edge(s) have no Service VPN "
            "interface with a usable IPv4 address and are NOT drawn in this "
            "overlay at all (rather than appearing as isolated nodes). They "
            "are still present in the underlay diagram."
        )
    caveats.append(
        "MODEL -- each Edge's NS area is named after its reported 'site-name' "
        "('br1'), falling back to 'site<id>' when the export carries no name "
        "for that site (and to '<name>_site<id>' for every site-id involved "
        "when two different site-ids report the SAME name). The underlay "
        "diagram uses the identical area names."
    )
    caveats.append(
        "OUT OF SCOPE (by design) -- OMP-advertised routes, service chaining "
        "/ service insertion, and centralized control policy are NOT drawn. "
        "Controllers (vManage/vSmart/vBond) are also absent from this overlay "
        "(orchestration/control-plane, not a Service VPN endpoint)."
    )
    if not plan.edges:
        caveats.append(
            "No Service VPN interfaces with a resolvable IPv4 address were "
            "found on any SD-WAN Edge; the overlay diagram is empty. Check "
            "that the export's interface records carry a non-zero 'vpn-id'."
        )

    info = {"mappings": mappings, "counts": counts, "caveats": caveats}
    return model, info
