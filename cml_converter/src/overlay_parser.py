# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0
"""Find the overlay a node's saved config describes.

Three overlays are recognised, each from its own config stanzas:

* VXLAN EVPN -- NX-OS ``vlan N`` / ``vn-segment``, ``vrf context`` / ``vni``,
  ``interface nve1`` / ``member vni``; IOS-XE ``vlan configuration N`` /
  ``member evpn-instance E vni X`` and ``interface nve1`` /
  ``member vni X vrf V``.
* SD-WAN (Catalyst cEdge) -- ``system`` / ``system-ip`` / ``site-id``, the
  ``sdwan`` stanza's ``tunnel-interface`` ports (or ``interface TunnelN`` with
  ``tunnel mode sdwan``), and the numbered ``vrf definition`` Service VPNs.
* L3VPN (MPLS / SR-MPLS / SRv6) -- a VPN address family under ``router bgp``
  (``vpnv4`` / ``vpnv6``) together with a VRF bound to at least one port.

Only the saved config is read; nothing here talks to a controller.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple

from .config_parser import ParsedConfig, _indent, _top_blocks
from .topology_mapper import normalise_port_name

OVERLAY_TYPES = ("evpn", "sdwan", "l3vpn")
OVERLAY_TITLES = {"evpn": "VXLAN EVPN", "sdwan": "SD-WAN", "l3vpn": "L3VPN"}

_MGMT_VRFS = {"management", "mgmt-intf", "mgmt", "mgmt_vrf", "65528", "512"}


@dataclass
class PortContext:
    """What the overlay needs to know about one port."""
    port: str                       # NS port name, e.g. 'GigabitEthernet 3'
    kind: str                       # ParsedInterface.kind
    vrf: Optional[str] = None
    cidrs: List[str] = field(default_factory=list)
    access_vlan: Optional[int] = None


@dataclass
class EvpnFacts:
    l2vni: Dict[int, int] = field(default_factory=dict)      # vlan -> L2VNI
    l3vni: Dict[str, int] = field(default_factory=dict)      # vrf -> L3VNI
    vlan_names: Dict[int, str] = field(default_factory=dict)
    anycast_vlans: Set[int] = field(default_factory=set)     # SVIs with anycast GW


@dataclass
class SdwanFacts:
    system_ip: Optional[str] = None
    site_id: Optional[int] = None
    vpns: Dict[int, str] = field(default_factory=dict)       # VPN id -> VRF name
    transport_ports: Dict[str, str] = field(default_factory=dict)  # port -> colour


@dataclass
class L3vpnFacts:
    vrfs: Dict[str, Dict[str, List[str]]] = field(default_factory=dict)  # vrf -> rd/import/export
    families: List[str] = field(default_factory=list)        # vpnv4 / vpnv6
    transport: List[str] = field(default_factory=list)       # LDP / SR-MPLS / SRv6 ...


@dataclass
class OverlayFacts:
    device: str
    os_family: str = "unknown"
    evpn: Optional[EvpnFacts] = None
    sdwan: Optional[SdwanFacts] = None
    l3vpn: Optional[L3vpnFacts] = None
    ports: Dict[str, PortContext] = field(default_factory=dict)

    def types(self) -> List[str]:
        return [t for t in OVERLAY_TYPES if getattr(self, t) is not None]


def is_mgmt_vrf(vrf: Optional[str]) -> bool:
    return bool(vrf) and vrf.lower() in _MGMT_VRFS


def _ns_port(name: str) -> str:
    return normalise_port_name(name) or name


def _port_contexts(parsed: ParsedConfig) -> Dict[str, PortContext]:
    out: Dict[str, PortContext] = {}
    for iface in parsed.interfaces.values():
        if iface.shutdown and not iface.has_ip():
            continue
        port = _ns_port(iface.name)
        vrf = iface.vrf if iface.vrf and not is_mgmt_vrf(iface.vrf) else None
        out[port] = PortContext(
            port=port, kind=iface.kind, vrf=vrf,
            cidrs=list(iface.all_cidrs()), access_vlan=iface.access_vlan,
        )
    return out


def _children(body: List[str]) -> List[Tuple[str, List[str]]]:
    """Direct children of a block (shallowest indent) with their own bodies."""
    if not body:
        return []
    depth = min(_indent(l) for l in body)
    out: List[Tuple[str, List[str]]] = []
    for line in body:
        if _indent(line) == depth:
            out.append((line.strip(), []))
        elif out:
            out[-1][1].append(line)
    return out


def _words(line: str) -> List[str]:
    return line.strip().split()


def _vlan_ids(token: str) -> List[int]:
    ids: List[int] = []
    for part in token.split(","):
        part = part.strip()
        if re.fullmatch(r"\d+", part):
            ids.append(int(part))
    return ids


# ---------------------------------------------------------------------------
# VXLAN EVPN
# ---------------------------------------------------------------------------

def _parse_evpn(blocks: List[Tuple[str, List[str]]], parsed: ParsedConfig) -> Optional[EvpnFacts]:
    facts = EvpnFacts()
    vlan_vni: Dict[int, int] = {}
    nve_l2: Set[int] = set()
    nve_l3: Dict[int, Optional[str]] = {}
    evpn_seen = False

    for head, body in blocks:
        w = _words(head)
        low = [x.lower() for x in w]
        if low[:1] == ["vlan"] and len(w) >= 2 and low[1] != "configuration":
            vids = _vlan_ids(w[1])
            for line in body:
                c = _words(line)
                if len(c) >= 2 and c[0].lower() == "vn-segment" and c[1].isdigit():
                    for vid in vids:
                        vlan_vni[vid] = int(c[1])
                elif len(c) >= 2 and c[0].lower() == "name":
                    for vid in vids:
                        facts.vlan_names[vid] = c[1]
        elif low[:2] == ["vlan", "configuration"] and len(w) >= 3:
            vids = _vlan_ids(w[2])
            for line in body:
                m = re.search(r"\bmember\s+(?:evpn-instance\s+\d+\s+)?vni\s+(\d+)", line, re.I)
                if m:
                    for vid in vids:
                        vlan_vni[vid] = int(m.group(1))
        elif low[:2] == ["vrf", "context"] and len(w) >= 3:
            for line in body:
                c = _words(line)
                if len(c) >= 2 and c[0].lower() == "vni" and c[1].isdigit():
                    facts.l3vni[w[2]] = int(c[1])
        elif low[:1] == ["interface"] and len(w) >= 2 and re.match(r"(?i)nve\s*\d", " ".join(w[1:])):
            for line in body:
                m = re.match(r"\s*member\s+vni\s+(\d+)(.*)$", line, re.I)
                if not m:
                    continue
                vni, rest = int(m.group(1)), m.group(2)
                vm = re.search(r"\bvrf\s+(\S+)", rest, re.I)
                if vm or re.search(r"\bassociate-vrf\b", rest, re.I):
                    nve_l3[vni] = vm.group(1) if vm else None
                else:
                    nve_l2.add(vni)
        elif low[:2] == ["nv", "overlay"] or low[:3] == ["feature", "nv", "overlay"]:
            evpn_seen = True
        elif low[:2] == ["l2vpn", "evpn"]:
            evpn_seen = True
        elif low[:1] == ["interface"] and len(w) >= 2:
            name = " ".join(w[1:])
            m = re.match(r"(?i)vl(?:an)?\s*(\d+)$", name)
            if m and any("anycast-gateway" in l.lower() for l in body):
                facts.anycast_vlans.add(int(m.group(1)))

    for vni, vrf in nve_l3.items():
        if vrf:
            facts.l3vni.setdefault(vrf, vni)
    l3_vnis = set(facts.l3vni.values()) | set(nve_l3)
    for vid, vni in vlan_vni.items():
        if vni in l3_vnis:
            # An NX-OS L3VNI also has a VLAN; name the VRF from it when only
            # 'associate-vrf' was given.
            continue
        if nve_l2 and vni not in nve_l2:
            continue
        facts.l2vni[vid] = vni
    for vid in facts.l2vni:
        facts.vlan_names.setdefault(vid, parsed.vlans.get(vid, "") or "")
    if not facts.l2vni and not facts.l3vni:
        return None
    if not (nve_l2 or nve_l3) and not evpn_seen:
        return None
    return facts


# ---------------------------------------------------------------------------
# SD-WAN (cEdge)
# ---------------------------------------------------------------------------

def _parse_sdwan(blocks: List[Tuple[str, List[str]]], parsed: ParsedConfig) -> Optional[SdwanFacts]:
    facts = SdwanFacts()
    sdwan_seen = False
    for head, body in blocks:
        w = _words(head)
        low = [x.lower() for x in w]
        if low == ["system"]:
            for line in body:
                c = _words(line)
                if len(c) >= 2 and c[0].lower() == "system-ip":
                    facts.system_ip = c[1]
                elif len(c) >= 2 and c[0].lower() == "site-id" and c[1].isdigit():
                    facts.site_id = int(c[1])
        elif low == ["sdwan"]:
            sdwan_seen = True
            for child, sub in _children(body):
                cw = _words(child)
                if len(cw) < 2 or cw[0].lower() != "interface":
                    continue
                if not any(l.strip().lower() == "tunnel-interface" for l in sub):
                    continue
                colour = ""
                for l in sub:
                    lw = _words(l)
                    if len(lw) >= 2 and lw[0].lower() == "color":
                        colour = lw[1]
                facts.transport_ports[_ns_port(" ".join(cw[1:]))] = colour
        elif low[:1] == ["interface"] and len(w) >= 2 and re.match(r"(?i)tun", w[1]):
            if any(re.match(r"\s*tunnel\s+mode\s+sdwan", l, re.I) for l in body):
                sdwan_seen = True
                for l in body:
                    m = re.match(r"\s*tunnel\s+source\s+(\S+)", l, re.I)
                    if m:
                        facts.transport_ports.setdefault(_ns_port(m.group(1)), "")
        elif low[:2] == ["vrf", "definition"] and len(w) >= 3:
            if w[2].isdigit() and not is_mgmt_vrf(w[2]) and w[2] != "0":
                facts.vpns[int(w[2])] = w[2]
    if not sdwan_seen or (facts.system_ip is None and facts.site_id is None):
        return None
    return facts


# ---------------------------------------------------------------------------
# L3VPN (MPLS / SR / SRv6)
# ---------------------------------------------------------------------------

def _route_targets(body: List[str]) -> Dict[str, List[str]]:
    """``rd`` / import / export route targets from a VRF stanza (IOS and XR)."""
    out: Dict[str, List[str]] = {"rd": [], "import": [], "export": []}
    xr_dir: Optional[str] = None
    for line in body:
        c = _words(line)
        if not c:
            continue
        key = c[0].lower()
        if key == "rd" and len(c) >= 2:
            out["rd"].append(c[1])
            xr_dir = None
        elif key == "route-target" and len(c) >= 3:
            dirs = ["import", "export"] if c[1].lower() == "both" else [c[1].lower()]
            for d in dirs:
                if d in out and c[2] not in out[d]:
                    out[d].append(c[2])
            xr_dir = None
        elif key in ("import", "export") and len(c) >= 2 and c[1].lower() == "route-target":
            xr_dir = key
            if len(c) >= 3 and c[2] not in out[key]:
                out[key].append(c[2])
        elif xr_dir and re.fullmatch(r"[\d.]+:\d+", c[0]):
            if c[0] not in out[xr_dir]:
                out[xr_dir].append(c[0])
        else:
            xr_dir = None
    return out


def _parse_l3vpn(
    blocks: List[Tuple[str, List[str]]],
    ports: Dict[str, PortContext],
) -> Optional[L3vpnFacts]:
    facts = L3vpnFacts()
    vrf_bodies: Dict[str, List[str]] = {}
    for head, body in blocks:
        w = _words(head)
        low = [x.lower() for x in w]
        if low[:2] == ["router", "bgp"]:
            for child, sub in _children(body):
                cw = [x.lower() for x in _words(child)]
                if cw[:1] == ["address-family"] and len(cw) >= 2 and cw[1] in ("vpnv4", "vpnv6"):
                    if cw[1] not in facts.families:
                        facts.families.append(cw[1])
                elif cw[:1] == ["vrf"] and len(cw) >= 2:
                    name = _words(child)[1]
                    for l in sub:
                        lw = _words(l)
                        if len(lw) >= 2 and lw[0].lower() == "rd":
                            vrf_bodies.setdefault(name, []).append(f" rd {lw[1]}")
        elif low[:2] in (["vrf", "definition"], ["ip", "vrf"]) and len(w) >= 3:
            vrf_bodies.setdefault(w[2], []).extend(body)
        elif low[:1] == ["vrf"] and len(w) == 2:
            vrf_bodies.setdefault(w[1], []).extend(body)
        elif low[:1] == ["segment-routing"]:
            text = " ".join(l.strip().lower() for l in body)
            if "srv6" in text:
                _add(facts.transport, "SRv6")
            if "mpls" in text or "global-block" in text or not text:
                _add(facts.transport, "SR-MPLS")
        elif low[:2] == ["mpls", "ldp"] or low[:4] == ["mpls", "label", "protocol", "ldp"]:
            _add(facts.transport, "LDP")
        elif low[:1] == ["interface"] and any(l.strip().lower() == "mpls ip" for l in body):
            _add(facts.transport, "LDP")
    if not facts.families:
        return None
    for name in sorted({p.vrf for p in ports.values() if p.vrf}):
        facts.vrfs[name] = _route_targets(vrf_bodies.get(name, []))
    if not facts.vrfs:
        return None
    return facts


def _add(items: List[str], value: str) -> None:
    if value not in items:
        items.append(value)


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def parse_overlay(device: str, raw_text: str, parsed: ParsedConfig) -> OverlayFacts:
    """Overlay facts for one node; every overlay field is None when absent."""
    blocks = _top_blocks((raw_text or "").splitlines())
    facts = OverlayFacts(device=device, os_family=parsed.os_family)
    facts.ports = _port_contexts(parsed)
    facts.evpn = _parse_evpn(blocks, parsed)
    facts.sdwan = _parse_sdwan(blocks, parsed)
    if facts.sdwan is None:
        facts.l3vpn = _parse_l3vpn(blocks, facts.ports)
    return facts


def parse_all_overlays(
    raw_configs: Dict[str, str],
    parsed_configs: Dict[str, ParsedConfig],
) -> Dict[str, OverlayFacts]:
    """``{label: OverlayFacts}`` for every node with an extracted config."""
    return {
        label: parse_overlay(label, raw_configs.get(label, ""), parsed)
        for label, parsed in parsed_configs.items()
    }


def detected_types(facts: Iterable[OverlayFacts]) -> List[str]:
    found: Set[str] = set()
    for f in facts:
        found.update(f.types())
    return [t for t in OVERLAY_TYPES if t in found]
