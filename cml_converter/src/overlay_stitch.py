# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0
"""Join the EVPN, SD-WAN and L3VPN overlays into one model and lay it out.

Each builder hands over its LAN-facing VRF / VPN ports as attachments. For
every attachment the CML L1 link on that port is followed:

* to another overlay's attachment -- a handoff (for example a border leaf
  VRF port cabled to a cEdge Service VPN port): the two drawn ends are
  linked, the VRF / VPN pair goes into both devices' Attribute-E, and the
  pair is ``confirmed`` when the two ports share a subnet, else ``inferred``;
* to a transparent L2 switch (a CML unmanaged switch, or a switch with no
  config): the switch is drawn and everything else cabled to it is resolved
  the same way, one level deep;
* to any other device (a CE / VRF-lite router, a host): that device is drawn
  below with its own IP and VRF on the port;
* to nothing: an SD-WAN port goes to a gray per-Edge LAN dummy as in
  sdwan_converter; any other port is reported and left out.

A link between two different site areas is never drawn (NS RULE 3); it is
listed in the report instead.
"""

from __future__ import annotations

import ipaddress
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from .overlay_canvas import (
    COLOR_SYNTHETIC, ROW_HOST, ROW_NETWORK, Attachment, Handoff, OverlayCanvas,
    logical_stencil,
)
from .overlay_evpn import build_evpn
from .overlay_l3vpn import build_l3vpn
from .overlay_parser import OVERLAY_TYPES, OverlayFacts, detected_types, parse_all_overlays
from .overlay_sdwan import build_sdwan
from .overlay_sites import assign_overlay_areas, wan_area
from .stencil_mapper import (
    NS_FIREWALL, NS_L3SWITCH, NS_ROUTER, NS_SWITCH, StencilMapping,
    model_is_external_connector,
)
from .topology_mapper import NSL1Link, NSModel, arrange_area_rows

_TRANSPARENT_NODES = {"unmanaged_switch"}


@dataclass
class OverlayResult:
    model: NSModel
    canvas: OverlayCanvas
    facts: Dict[str, OverlayFacts]
    found: List[str]                 # overlay types present in the configs
    drawn: List[str]                 # overlay types drawn (after --overlay)
    areas: Dict[str, str] = field(default_factory=dict)


def _networks(cidrs: Iterable[str]) -> Set[Any]:
    out: Set[Any] = set()
    for c in cidrs:
        try:
            out.add(ipaddress.ip_interface(c).network)
        except ValueError:
            continue
    return out


def _status(a: List[str], b: List[str]) -> str:
    return "confirmed" if _networks(a) & _networks(b) else "inferred"


class _Stitcher:
    def __init__(
        self,
        canvas: OverlayCanvas,
        facts: Dict[str, OverlayFacts],
        areas: Dict[str, str],
        lab_of: Dict[str, str],
        stencils: Dict[str, StencilMapping],
        l1_links: List[NSL1Link],
        attachments: List[Attachment],
    ) -> None:
        self.canvas = canvas
        self.facts = facts
        self.areas = areas
        self.lab_of = lab_of
        self.stencils = stencils
        self.index: Dict[Tuple[str, str], Attachment] = {}
        for a in attachments:
            self.index.setdefault((a.device, a.port), a)
        self.attachments = list(self.index.values())
        self.peers: Dict[Tuple[str, str], List[Tuple[str, str]]] = defaultdict(list)
        self.ports_of: Dict[str, List[str]] = defaultdict(list)
        for lk in l1_links:
            self.peers[(lk.a_device, lk.a_port)].append((lk.b_device, lk.b_port))
            self.peers[(lk.b_device, lk.b_port)].append((lk.a_device, lk.a_port))
            self.ports_of[lk.a_device].append(lk.a_port)
            self.ports_of[lk.b_device].append(lk.b_port)
        self.done: Set[Tuple[str, str]] = set()
        self.lan_dummy: Dict[str, str] = {}

    # ----- helpers -----
    def _transparent(self, device: str) -> bool:
        st = self.stencils.get(device)
        if st is None:
            return False
        if (st.node_definition or "").lower() in _TRANSPARENT_NODES:
            return True
        f = self.facts.get(device)
        has_ip = f is not None and any(p.cidrs for p in f.ports.values())
        return st.stencil_type == NS_SWITCH and not has_ip

    def _drawable(self, device: str) -> bool:
        st = self.stencils.get(device)
        return st is not None and not model_is_external_connector(st.model)

    def _place_real(self, device: str, row: int) -> bool:
        if self.canvas.has(device):
            return True
        if not self._drawable(device):
            return False
        st = self.stencils[device]
        attr = ""
        f = self.facts.get(device)
        vrfs = sorted({p.vrf for p in f.ports.values() if p.vrf}) if f else []
        if vrfs:
            attr = "VRF " + ",".join(vrfs)
        self.canvas.add_device(
            device, self.areas.get(device, self.lab_of.get(device, "lab")),
            self.lab_of.get(device, "lab"), row, st,
            attribute=attr, is_endpoint=st.stencil_type not in (NS_ROUTER, NS_L3SWITCH, NS_SWITCH, NS_FIREWALL),
        )
        return True

    def _peer_row(self, device: str) -> int:
        st = self.stencils.get(device)
        if st is not None and st.stencil_type in (NS_SWITCH, NS_L3SWITCH) and self._transparent(device):
            return ROW_NETWORK
        return ROW_HOST

    def _link(self, a: Tuple[str, str], b: Tuple[str, str], why: str) -> bool:
        if self.canvas.link(a[0], a[1], b[0], b[1]):
            return True
        if self.canvas.port_free(a[0], a[1]) and self.canvas.port_free(b[0], b[1]):
            self.canvas.notes.append(
                f"Not drawn ({why}): {a[0]} {a[1]} [{self.canvas.area_of(a[0])}] -- "
                f"{b[0]} {b[1]} [{self.canvas.area_of(b[0])}] would cross areas (RULE 3).")
        return False

    def _handoff(self, a: Attachment, b_dev: str, b_port: str, b_ctx: str,
                 b_cidrs: List[str], b_draw: Optional[str], drawn: bool, via: str = "") -> None:
        """Record a handoff between ``a`` and ``b`` and note it on both drawn ends."""
        status = _status(a.cidrs, b_cidrs)
        h = Handoff(a.device, a.port, a.context, b_dev, b_port, b_ctx, status,
                    drawn=drawn, note=f"via {via}" if via else "")
        self.canvas.handoffs.append(h)
        if drawn:
            self.canvas.add_handoff_note(a.draw_device, f"{a.port} {a.context} - {b_dev} {b_ctx} ({status})")
            if b_draw:
                self.canvas.add_handoff_note(b_draw, f"{b_port} {b_ctx} - {a.device} {a.context} ({status})")

    @staticmethod
    def _differs(a: Attachment, b: Attachment) -> bool:
        return a.overlay != b.overlay or a.vrf != b.vrf

    def _context_of(self, device: str, port: str) -> Tuple[str, List[str], Optional[str]]:
        f = self.facts.get(device)
        ctx = f.ports.get(port) if f else None
        if ctx is None:
            return "", [], None
        return (f"VRF {ctx.vrf}" if ctx.vrf else ""), list(ctx.cidrs), ctx.vrf

    # ----- one far end -----
    def _attach_peer(self, near: Tuple[str, str], q: str, qp: str) -> Optional[Tuple[str, str, str, List[str]]]:
        """Draw the non-overlay device ``q`` and link its port ``qp`` to ``near``.

        Returns ``(q, qp, context, cidrs)`` when that port is in a VRF (a CE /
        VRF-lite handoff), else None.
        """
        if not self._place_real(q, self._peer_row(q)):
            return None
        if not self._link(near, (q, qp), "LAN"):
            return None
        ctx, cidrs, vrf = self._context_of(q, qp)
        self.canvas.ip(q, qp, cidrs)
        self.canvas.vrf(q, qp, vrf)
        return (q, qp, ctx, cidrs) if vrf else None

    def _connect(self, a: Attachment, a_end: Tuple[str, str], q: str, qp: str) -> None:
        """Link ``a_end`` straight to ``q qp`` (no switch in between)."""
        b = self.index.get((q, qp))
        if b is not None:
            self.done.add((q, qp))
            ok = self._link(a_end, b.end(self.canvas), "handoff")
            if self._differs(a, b):
                self._handoff(a, b.device, b.port, b.context, b.cidrs, b.draw_device, ok)
            return
        peer = self._attach_peer(a_end, q, qp)
        if peer:
            self._handoff(a, peer[0], peer[1], peer[2], peer[3], peer[0], True)

    def _through_switch(self, a: Attachment, a_end: Tuple[str, str], sw: str, sw_port: str) -> None:
        """Draw ``sw`` and resolve everything cabled to it as one LAN segment."""
        if not self._place_real(sw, ROW_NETWORK):
            return
        if not self._link(a_end, (sw, sw_port), "LAN"):
            return
        members: List[Attachment] = [a]
        vrf_peers: List[Tuple[str, str, str, List[str]]] = []
        for port in sorted(set(self.ports_of[sw])):
            if port == sw_port or not self.canvas.port_free(sw, port):
                continue
            for q, qp in self.peers.get((sw, port), []):
                if (q, qp) == (a.device, a.port):
                    continue
                b = self.index.get((q, qp))
                if b is not None:
                    self.done.add((q, qp))
                    if self._link(b.end(self.canvas), (sw, port), "LAN"):
                        members.append(b)
                    continue
                peer = self._attach_peer((sw, port), q, qp)
                if peer:
                    vrf_peers.append(peer)
        for i, x in enumerate(members):
            for y in members[i + 1:]:
                if self._differs(x, y):
                    self._handoff(x, y.device, y.port, y.context, y.cidrs, y.draw_device, True, sw)
            for q, qp, ctx, cidrs in vrf_peers:
                self._handoff(x, q, qp, ctx, cidrs, q, True, sw)

    def _to_lan_dummy(self, a: Attachment, a_end: Tuple[str, str]) -> None:
        name = self.lan_dummy.get(a.device)
        if name is None:
            name = self.canvas.unique_name(f"LAN:{a.device}")
            area = self.canvas.area_of(a.device) or self.areas.get(a.device, "lab")
            self.canvas.add_device(
                name, area, self.lab_of.get(a.device, "lab"), ROW_NETWORK,
                logical_stencil(name, "lan-dummy", NS_SWITCH, "LAN segment (no CML link)"),
                color=COLOR_SYNTHETIC,
            )
            self.lan_dummy[a.device] = name
        dport = self.canvas.dummy(name)
        if self.canvas.link(a_end[0], a_end[1], name, dport):
            self.canvas.vrf(name, dport, a.vrf)
            self.canvas.counts["lan_dummy_ports"] += 1

    # ----- all -----
    def run(self) -> None:
        for a in self.attachments:
            key = (a.device, a.port)
            if key in self.done:
                continue
            self.done.add(key)
            far = self.peers.get(key, [])
            if not far:
                if a.lan_dummy:
                    self._to_lan_dummy(a, a.end(self.canvas))
                else:
                    self.canvas.notes.append(
                        f"{a.device} {a.port} ({a.context}) has no CML link and is not drawn.")
                continue
            a_end = a.end(self.canvas)
            for q, qp in far:
                if (q, qp) not in self.index and self._transparent(q):
                    self._through_switch(a, a_end, q, qp)
                else:
                    self._connect(a, a_end, q, qp)


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

def _site_key(area: str, lab: str) -> Tuple[int, int, str]:
    tail = area[len(lab) + 1:] if area.startswith(lab + "-") else ""
    if tail.startswith("site") and tail[4:].isdigit():
        return (0, int(tail[4:]), area)
    if area == lab:
        return (2, 0, area)
    return (1, 0, area)


def layout(canvas: OverlayCanvas, lab_order: List[str]) -> None:
    """Per lab, the WAN waypoint row above that lab's site areas."""
    by_lab: Dict[str, List[str]] = defaultdict(list)
    for area, lab in canvas.lab_of_area.items():
        if any(canvas.rows[area].values()):
            by_lab[lab].append(area)
    labs = [l for l in lab_order if l in by_lab] + sorted(l for l in by_lab if l not in lab_order)
    ordered_rows: List[List[str]] = []
    for lab in labs:
        areas = by_lab[lab]
        wan = wan_area(lab)
        if wan in areas:
            ordered_rows.append([wan])
        sites = sorted((a for a in areas if a != wan), key=lambda a: _site_key(a, lab))
        if sites:
            ordered_rows.extend(arrange_area_rows(sites))
    canvas.model.areas = ordered_rows
    canvas.model.area_to_devices = {}
    for row in ordered_rows:
        for area in row:
            grid = [names for _, names in sorted(canvas.rows[area].items()) if names]
            if grid:
                canvas.model.area_to_devices[area] = grid


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def build_overlay_model(
    nodes: List[Dict[str, Any]],
    l1_links: List[NSL1Link],
    stencils: Dict[str, StencilMapping],
    raw_configs: Dict[str, str],
    parsed_configs: Dict[str, Any],
    default_lab: str,
    wanted: Optional[List[str]] = None,
) -> OverlayResult:
    """The integrated overlay of every lab in ``nodes``.

    ``wanted`` narrows the overlay types drawn (None or ``auto`` = all found).
    """
    facts = parse_all_overlays(raw_configs, parsed_configs)
    found = detected_types(facts.values())
    want = [t for t in OVERLAY_TYPES if not wanted or "auto" in wanted or t in wanted]
    for f in facts.values():
        for t in OVERLAY_TYPES:
            if t not in want:
                setattr(f, t, None)
    drawn = detected_types(facts.values())

    labels = [str(n.get("label", "")) for n in nodes if n.get("label")]
    lab_of: Dict[str, str] = {}
    lab_order: List[str] = []
    for n in nodes:
        label = str(n.get("label", ""))
        if not label:
            continue
        stamped = n.get("_ns_area")
        lab = stamped.strip() if isinstance(stamped, str) and stamped.strip() else default_lab
        lab_of[label] = lab
        if lab not in lab_order:
            lab_order.append(lab)

    canvas = OverlayCanvas()
    canvas.seed_names(labels)
    if not drawn:
        return OverlayResult(canvas.model, canvas, facts, found, drawn)

    areas = assign_overlay_areas(labels, lab_of, l1_links, facts)
    attachments: List[Attachment] = []
    attachments += build_sdwan(canvas, facts, areas, lab_of, stencils)
    evpn = build_evpn(canvas, facts, areas, lab_of, stencils, l1_links)
    attachments += evpn
    attachments += build_l3vpn(canvas, facts, areas, lab_of, stencils,
                               taken={(a.device, a.port) for a in evpn})
    _Stitcher(canvas, facts, areas, lab_of, stencils, l1_links, attachments).run()

    pruned = canvas.prune_missing_ports()
    if pruned:
        canvas.notes.append(f"{pruned} IP / VRF / L2 item(s) dropped because their port was not drawn.")
    layout(canvas, lab_order)
    return OverlayResult(canvas.model, canvas, facts, found, drawn, areas)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def write_overlay_report(path: Any, result: OverlayResult, not_extracted: List[str]) -> None:
    from .overlay_parser import OVERLAY_TITLES

    lines: List[str] = ["# CML overlay report", ""]
    lines.append("Overlay found in the configs: " + (
        ", ".join(OVERLAY_TITLES[t] for t in result.found) or "none"))
    lines.append("Overlay drawn: " + (", ".join(OVERLAY_TITLES[t] for t in result.drawn) or "none"))
    lines.append("")
    lines.append("## Devices with an overlay")
    lines.append("")
    lines.append("| Device | Overlay | Area | Detail |")
    lines.append("|---|---|---|---|")
    for label, f in sorted(result.facts.items()):
        types = f.types()
        if not types:
            continue
        detail: List[str] = []
        if f.evpn:
            detail.append(f"L2VNI {len(f.evpn.l2vni)}, L3VNI {len(f.evpn.l3vni)}")
        if f.sdwan:
            detail.append(f"system-ip {f.sdwan.system_ip}, site-id {f.sdwan.site_id}, "
                          f"VPN {','.join(str(v) for v in sorted(f.sdwan.vpns)) or '-'}")
        if f.l3vpn:
            detail.append(f"{','.join(f.l3vpn.families)} {','.join(f.l3vpn.transport)} "
                          f"VRF {','.join(sorted(f.l3vpn.vrfs))}")
        lines.append(f"| {label} | {', '.join(OVERLAY_TITLES[t] for t in types)} | "
                     f"{result.areas.get(label, '')} | {'; '.join(detail)} |")
    lines.append("")
    lines.append("## Handoffs")
    lines.append("")
    if result.canvas.handoffs:
        lines.append("| A | A context | B | B context | Mapping | Drawn | Note |")
        lines.append("|---|---|---|---|---|---|---|")
        for h in result.canvas.handoffs:
            lines.append(f"| {h.a_device} {h.a_port} | {h.a_context} | {h.b_device} {h.b_port} | "
                         f"{h.b_context} | {h.status} | {'yes' if h.drawn else 'no'} | {h.note} |")
        lines.append("")
        lines.append("`confirmed`: both ports are in the same subnet. `inferred`: they are cabled "
                     "together in CML but the subnets differ or one side has no IP.")
    else:
        lines.append("None.")
    lines.append("")
    lines.append("## Areas")
    lines.append("")
    for row in result.model.areas:
        lines.append("- " + " | ".join(row))
    if result.canvas.notes:
        lines.append("")
        lines.append("## Notes")
        lines.append("")
        lines.extend(f"- {n}" for n in result.canvas.notes)
    if not_extracted:
        lines.append("")
        lines.append("## Config not extracted (day-0)")
        lines.append("")
        lines.append("These nodes still carry CML's day-0 config, so their overlay cannot be read. "
                     "Run *Extract Configs* in CML (or `fetch_from_cml` after the lab has booted) and "
                     "convert again.")
        lines.append("")
        lines.extend(f"- {n}" for n in not_extracted)
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
