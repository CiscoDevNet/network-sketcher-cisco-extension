# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""Parse Cisco NX-OS / IOS / IOS-XE running-configs into a normalised model.

The parser captures what NS can express directly (VLANs, SVIs, Loopbacks,
L3-routed physical interfaces, sub-interfaces, port-channels, VRFs, trunk
allowed VLANs, access VLAN, channel-group membership) and captures everything
else (BGP, OSPF, EVPN, NVE, HSRP, PIM, etc.) as a free-text routing summary
that will be stored on the NS device as an attribute string.

Designed to be tolerant of incomplete configs; never raises on a single bad
line. Each input config produces a ParsedConfig instance and a list of
fall-through (unrecognised) lines that the assessment phase counts.
"""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

try:
    from ciscoconfparse2 import CiscoConfParse  # type: ignore
    _HAVE_CCP = True
except Exception:  # pragma: no cover - fallback
    CiscoConfParse = None  # type: ignore
    _HAVE_CCP = False


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class IPv4Addr:
    address: str  # dotted-quad, no prefix
    prefix: int  # CIDR mask

    @property
    def cidr(self) -> str:
        return f"{self.address}/{self.prefix}"


@dataclass
class ParsedInterface:
    name: str                          # e.g. "Ethernet1/1", "Vlan10", "Loopback0", "Port-channel1"
    kind: str                          # 'physical' | 'svi' | 'loopback' | 'subif' | 'portchannel' | 'mgmt' | 'tunnel' | 'unknown'
    description: Optional[str] = None
    shutdown: bool = False
    switchport: bool = True            # NX-OS default is L3 except on N9K-vlan; we set True only when seen
    no_switchport_seen: bool = False
    mode: Optional[str] = None         # 'access' | 'trunk' | None
    access_vlan: Optional[int] = None
    trunk_native_vlan: Optional[int] = None
    trunk_allowed_vlans: List[int] = field(default_factory=list)
    ipv4: List[IPv4Addr] = field(default_factory=list)
    ipv4_secondary: List[IPv4Addr] = field(default_factory=list)
    vrf: Optional[str] = None
    channel_group: Optional[int] = None
    channel_mode: Optional[str] = None  # 'active' | 'passive' | 'on' | etc
    mtu: Optional[int] = None
    speed: Optional[str] = None
    ospf_area: Optional[str] = None

    def is_routed(self) -> bool:
        if self.kind in {"svi", "loopback", "mgmt", "tunnel"}:
            return True
        if self.kind in {"subif"}:
            return True
        if self.kind in {"portchannel", "physical"}:
            return self.no_switchport_seen or bool(self.ipv4)
        return bool(self.ipv4)


@dataclass
class ParsedConfig:
    hostname: Optional[str] = None
    os_family: str = "unknown"          # 'nxos' | 'ios' | 'iosxe' | 'iosxr' | 'unknown'
    vlans: Dict[int, str] = field(default_factory=dict)   # id -> name
    vrfs: Set[str] = field(default_factory=set)
    interfaces: Dict[str, ParsedInterface] = field(default_factory=dict)
    routing_summary_lines: List[str] = field(default_factory=list)
    raw_size_bytes: int = 0
    fall_through_count: int = 0
    parsed_line_count: int = 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Interface-kind classification. Mirrors the breadth of ``normalise_port_name``
# in topology_mapper so that every interface NS can represent is recognised here
# too. A type token must be followed by a digit (``(?=\d)``) so we never match a
# bare keyword. Crucially, physical types are matched WITH OR WITHOUT a slash
# (IOL/IOU labs use ``Ethernet0/0``/``e0/0`` while routers may use ``gig1``),
# and ``Management`` (not just ``mgmt``) and single-letter ``e``/``g`` are
# covered — these were previously dropped as ``unknown`` and lost their IPs.
#
# The ``(?=\s*\d)`` lookahead tolerates an optional space between the type token
# and the interface number — IOS accepts (and some dumps emit) both
# ``Ethernet0/1`` and ``Ethernet 0/1`` / ``Vlan 1`` / ``Loopback 99``.
_IFACE_KIND_RE = [
    (re.compile(r"^vl(?:an)?(?=\s*\d)", re.IGNORECASE), "svi"),
    (re.compile(r"^(?:loopback|loop|lo)(?=\s*\d)", re.IGNORECASE), "loopback"),
    (re.compile(r"^(?:management|mgmt)(?=\s*\d)", re.IGNORECASE), "mgmt"),
    (re.compile(r"^tun(?:nel)?(?=\s*\d)", re.IGNORECASE), "tunnel"),
    (re.compile(r"^nve(?=\s*\d)", re.IGNORECASE), "tunnel"),
    (re.compile(r"^(?:port-?channel|po)(?=\s*\d)", re.IGNORECASE), "portchannel"),
    (re.compile(
        r"^(?:twentyfivegige|twe|fortygigabitethernet|fortygige|fo|"
        r"hundredgige|hu|tengigabitethernet|tengige|te|"
        r"gigabitethernet|gige|gig|gi|fastethernet|fas|fa|"
        r"ethernet|eth|et|serial|ser|se|e|g)(?=\s*\d)",
        re.IGNORECASE), "physical"),
]


def _iface_kind(name: str) -> str:
    if "." in name:
        return "subif"
    for pat, kind in _IFACE_KIND_RE:
        if pat.search(name):
            return kind
    return "unknown"


def _parse_ip_cidr(s: str) -> Optional[IPv4Addr]:
    # Forms supported (trailing tokens such as ASA "standby x.x.x.x" are ignored
    # — we always take the leading address + mask/prefix):
    #   "10.0.0.1/24"                      (NX-OS)
    #   "10.0.0.1 255.255.255.0"           (IOS / IOS-XE)
    #   "10.0.0.1 255.255.255.0 standby 10.0.0.2" (ASA)
    s = s.strip()
    m = re.match(r"^(\d+\.\d+\.\d+\.\d+)/(\d+)", s)
    if m:
        return IPv4Addr(address=m.group(1), prefix=int(m.group(2)))
    m = re.match(r"^(\d+\.\d+\.\d+\.\d+)\s+(\d+\.\d+\.\d+\.\d+)", s)
    if m:
        try:
            net = ipaddress.IPv4Network(f"{m.group(1)}/{m.group(2)}", strict=False)
            return IPv4Addr(address=m.group(1), prefix=net.prefixlen)
        except (ipaddress.AddressValueError, ValueError):
            return None
    return None


def _expand_vlan_list(s: str) -> List[int]:
    """Expand 'switchport trunk allowed vlan' style ranges (1-10,20,30-32)."""
    out: List[int] = []
    s = s.strip()
    if not s or s.lower() in {"none", "all"}:
        return out
    for token in s.split(","):
        token = token.strip()
        if "-" in token:
            try:
                a, b = token.split("-", 1)
                out.extend(range(int(a), int(b) + 1))
            except ValueError:
                continue
        else:
            try:
                out.append(int(token))
            except ValueError:
                continue
    return sorted(set(out))


# Sentinels: lines we deliberately ignore (counted as parsed but no action).
_IGNORED_PREFIXES = (
    "!", "#", "version ", "boot ", "service ", "no service ", "no ip ", "ip ",
    "ipv6 ", "no ipv6 ", "logging ", "no logging ", "username ", "password ",
    "snmp-server ", "rmon ", "no password", "control-plane", "line ",
    "exec-timeout", "transport ", "stopbits", "no exec", "domain-lookup",
    "domain-name", "name-server", "spanning-tree", "no spanning-tree", "policy-map",
    "class-map", "policy-template", "system ", "feature ", "no feature ",
    "errdisable ", "ntp ", "clock ", "logfile", "session-limit", "license",
    "macro ", "vstack", "diagnostic ", "archive", "memory ", "no memory ",
    "no aaa", "aaa ", "redundancy", "service-policy",
)


def _line_is_ignored(stripped: str) -> bool:
    if not stripped:
        return True
    if stripped.startswith(("!", "#")):
        return True
    for prefix in _IGNORED_PREFIXES:
        if stripped.startswith(prefix):
            return True
    return False


# ---------------------------------------------------------------------------
# Main parse function
# ---------------------------------------------------------------------------

def detect_os_family(raw_text: str) -> str:
    """Heuristic OS family detection from a single running-config blob."""
    head = raw_text[:2000].lower()
    if "vdc " in head or "feature nv overlay" in head or "feature ospf" in head and "nxos" in head:
        return "nxos"
    if "version 10" in head and ("nv overlay" in head or "vdc " in head):
        return "nxos"
    if "interface gigabitethernet0/0" in head or "version 16" in head or "platform " in head and "version 17" in head:
        return "iosxe"
    if "ios xr" in head or "rp/" in head:
        return "iosxr"
    if "version 15" in head and "ios" in head:
        return "ios"
    # Best-effort fallbacks
    if "feature " in head:
        return "nxos"
    if "version " in head and "boot-start-marker" in head:
        return "iosxe"
    return "ios"


def parse_running_config(raw_text: str, hostname_hint: Optional[str] = None) -> ParsedConfig:
    """Parse a single device running-config into a ParsedConfig."""
    parsed = ParsedConfig(hostname=hostname_hint, raw_size_bytes=len(raw_text or ""))
    if not raw_text:
        return parsed

    parsed.os_family = detect_os_family(raw_text)

    if _HAVE_CCP:
        ccp = CiscoConfParse(raw_text.splitlines(), syntax="nxos" if parsed.os_family == "nxos" else "ios")
        _parse_with_ccp(parsed, ccp)
    else:
        _parse_with_regex(parsed, raw_text)

    # Interface stanzas are extracted with an indentation-agnostic section scan
    # (see _extract_interfaces_sectionwise). Some IOL/CSR ``show running-config``
    # dumps embedded in CML labs flatten interface child lines to column 0, which
    # defeats both ciscoconfparse's hierarchy and the indent-based regex parser;
    # the section scan recovers those interface IP/L2 settings regardless.
    _extract_interfaces_sectionwise(parsed, raw_text)

    # Linux endpoint hosts (CML "desktop"/"alpine"/"ubuntu" nodes) carry their
    # config as a boot shell script, not Cisco CLI; their IP lives on
    # ``ip addr add <cidr> dev <nic>`` lines. Recover those so host IPs are not
    # silently dropped (the gateway in ``ip route ... via`` is intentionally
    # ignored — it is not the host's own address).
    _extract_linux_host_ips(parsed, raw_text)

    # Collect routing summary (BGP/OSPF/EVPN/NVE/HSRP/PIM/EIGRP/ISIS sections).
    _collect_routing_summary(parsed, raw_text)
    return parsed


# ---------------------------------------------------------------------------
# CiscoConfParse-based path
# ---------------------------------------------------------------------------

def _parse_with_ccp(parsed: ParsedConfig, ccp) -> None:
    for line in ccp.find_objects(r"^hostname\s+"):
        parts = line.text.strip().split(maxsplit=1)
        if len(parts) == 2:
            parsed.hostname = parts[1]

    # VLAN definitions: NX-OS "vlan 10" then optional "name X"; IOS "vlan 10" same.
    for vlan_obj in ccp.find_objects(r"^vlan\s+\d+(?:,\d+|\s|\s*$)"):
        m = re.match(r"^vlan\s+([\d,\-\s]+)", vlan_obj.text)
        if not m:
            continue
        ids = _expand_vlan_list(m.group(1))
        name: Optional[str] = None
        for child in vlan_obj.children:
            if child.text.strip().startswith("name "):
                name = child.text.strip().split(maxsplit=1)[1]
        for vid in ids:
            parsed.vlans[vid] = name or parsed.vlans.get(vid, "")

    # VRF: NX-OS 'vrf context X'; IOS-XE 'vrf definition X'; IOS legacy 'ip vrf X'.
    for vrf_obj in ccp.find_objects(r"^vrf\s+(?:context|definition)\s+\S+|^ip\s+vrf\s+\S+"):
        parts = vrf_obj.text.strip().split()
        if parts and parts[-1] != "":
            parsed.vrfs.add(parts[-1])

    # NOTE: interfaces are NOT extracted here — see _extract_interfaces_sectionwise,
    # which is indentation-agnostic and handles flattened config dumps too.

    # Track parsed line counts.
    parsed.parsed_line_count = len(ccp.objs) if hasattr(ccp, "objs") else 0


# ---------------------------------------------------------------------------
# Regex fallback (used only if ciscoconfparse2 is unavailable)
# ---------------------------------------------------------------------------

def _parse_with_regex(parsed: ParsedConfig, raw_text: str) -> None:
    current_iface: Optional[ParsedInterface] = None
    current_vlan_block: Optional[List[int]] = None

    for line in raw_text.splitlines():
        raw = line.rstrip()
        if not raw:
            current_iface = None
            current_vlan_block = None
            continue
        stripped = raw.lstrip()
        indent = len(raw) - len(stripped)

        if indent == 0:
            current_iface = None
            current_vlan_block = None

            m = re.match(r"^hostname\s+(\S+)", stripped)
            if m:
                parsed.hostname = m.group(1)
                parsed.parsed_line_count += 1
                continue
            m = re.match(r"^vlan\s+([\d,\-\s]+)\s*$", stripped)
            if m:
                current_vlan_block = _expand_vlan_list(m.group(1))
                for vid in current_vlan_block:
                    parsed.vlans.setdefault(vid, "")
                parsed.parsed_line_count += 1
                continue
            m = re.match(r"^(?:vrf\s+(?:context|definition)|ip\s+vrf)\s+(\S+)", stripped)
            if m:
                parsed.vrfs.add(m.group(1))
                parsed.parsed_line_count += 1
                continue
            m = re.match(r"^interface\s+(\S+)", stripped)
            if m:
                name = m.group(1)
                current_iface = ParsedInterface(name=name, kind=_iface_kind(name))
                parsed.interfaces[name] = current_iface
                parsed.parsed_line_count += 1
                continue
            if _line_is_ignored(stripped):
                parsed.parsed_line_count += 1
                continue
            parsed.fall_through_count += 1
        else:
            # Indented child line
            if current_iface is not None:
                if _consume_iface_line(stripped, current_iface):
                    parsed.parsed_line_count += 1
                else:
                    parsed.fall_through_count += 1
            elif current_vlan_block is not None:
                m = re.match(r"^name\s+(.+)$", stripped)
                if m:
                    for vid in current_vlan_block:
                        parsed.vlans[vid] = m.group(1).strip()
                parsed.parsed_line_count += 1
            else:
                if _line_is_ignored(stripped):
                    parsed.parsed_line_count += 1
                else:
                    parsed.fall_through_count += 1


# ---------------------------------------------------------------------------
# Interface child-line consumer (shared by both paths)
# ---------------------------------------------------------------------------

def _consume_iface_line(stripped: str, iface: ParsedInterface) -> bool:
    """Return True iff we consumed the line into the iface structure."""
    if stripped.startswith("!") or stripped.startswith("#"):
        return True

    m = re.match(r"^description\s+(.+)$", stripped)
    if m:
        iface.description = m.group(1).strip()
        return True

    if stripped == "shutdown":
        iface.shutdown = True
        return True
    if stripped == "no shutdown":
        iface.shutdown = False
        return True

    if stripped == "no switchport":
        iface.no_switchport_seen = True
        iface.switchport = False
        return True
    if stripped == "switchport":
        iface.switchport = True
        return True

    m = re.match(r"^switchport\s+mode\s+(access|trunk)$", stripped)
    if m:
        iface.mode = m.group(1)
        iface.switchport = True
        return True
    m = re.match(r"^switchport\s+access\s+vlan\s+(\d+)$", stripped)
    if m:
        iface.access_vlan = int(m.group(1))
        iface.mode = iface.mode or "access"
        iface.switchport = True
        return True
    m = re.match(r"^switchport\s+trunk\s+native\s+vlan\s+(\d+)$", stripped)
    if m:
        iface.trunk_native_vlan = int(m.group(1))
        iface.mode = iface.mode or "trunk"
        iface.switchport = True
        return True
    m = re.match(r"^switchport\s+trunk\s+allowed\s+vlan(?:\s+(?:add|except|remove))?\s+(.+)$", stripped)
    if m:
        iface.trunk_allowed_vlans.extend(_expand_vlan_list(m.group(1)))
        iface.trunk_allowed_vlans = sorted(set(iface.trunk_allowed_vlans))
        iface.mode = iface.mode or "trunk"
        iface.switchport = True
        return True
    if stripped == "switchport trunk encapsulation dot1q":
        iface.switchport = True
        return True

    # encapsulation dot1q X (sub-interfaces on IOS-XE)
    m = re.match(r"^encapsulation\s+dot1q\s+(\d+)", stripped)
    if m:
        iface.access_vlan = int(m.group(1))  # used to derive l2_segment binding for sub-if
        return True

    m = re.match(r"^vrf\s+(?:member|forwarding)\s+(\S+)$", stripped)
    if m:
        iface.vrf = m.group(1)
        return True

    m = re.match(r"^ip\s+address\s+(.+?)(?:\s+secondary)?$", stripped)
    if m:
        addr = _parse_ip_cidr(m.group(1))
        if addr:
            if stripped.endswith(" secondary"):
                iface.ipv4_secondary.append(addr)
            else:
                iface.ipv4.append(addr)
            return True

    m = re.match(r"^channel-group\s+(\d+)(?:\s+mode\s+(\S+))?$", stripped)
    if m:
        iface.channel_group = int(m.group(1))
        iface.channel_mode = m.group(2)
        return True

    m = re.match(r"^mtu\s+(\d+)$", stripped)
    if m:
        iface.mtu = int(m.group(1))
        return True

    m = re.match(r"^speed\s+(\S+)$", stripped)
    if m:
        iface.speed = m.group(1)
        return True

    m = re.match(r"^ip\s+(?:router\s+)?ospf\s+\S+\s+area\s+(\S+)$", stripped)
    if m:
        iface.ospf_area = m.group(1)
        return True

    # Catch-all "we recognise this line as belonging to the interface stanza
    # but don't need to keep its semantics" -- still counted as parsed.
    if stripped.startswith((
        "ip ospf ", "ip pim", "ip nat", "ip helper",
        "ip access-group", "ipv6 ", "no ipv6 ",
        "service-policy", "storm-control", "media-type",
        "negotiation", "duplex", "load-interval", "lldp ",
        "logging event", "spanning-tree", "no spanning-tree", "bfd",
        "tx-queue-limit", "hold-queue",
        "carrier-delay", "platform ", "no platform ",
        "fabric forwarding", "no shutdown", "shutdown",
    )):
        return True

    return False


# ---------------------------------------------------------------------------
# Indentation-agnostic interface extraction
# ---------------------------------------------------------------------------

# A line that begins one of these top-level stanzas terminates the interface
# block currently being collected. This lets us delimit interface stanzas by
# *content* rather than indentation, so configs whose interface child lines are
# flattened to column 0 (common in some IOL/CSR dumps) still parse correctly.
# Only UNAMBIGUOUSLY top-level keywords belong here. Notably we must NOT list
# bare ``vrf`` (the interface child ``vrf forwarding|member <x>`` would falsely
# terminate the block before its ``ip address``), nor ``spanning-tree`` /
# ``monitor`` (both also appear as interface child lines). Interface stanzas are
# primarily delimited by the ``!`` separator / blank line; these keywords are a
# secondary safety net.
_SECTION_BOUNDARY_RE = re.compile(
    r"^(?:interface\b|router\b|line\b|vlan\b|"
    r"vrf\s+(?:context|definition)\b|ip\s+vrf\b|hostname\b|"
    r"control-plane\b|route-map\b|policy-map\b|class-map\b|"
    r"ip\s+access-list\b|crypto\b|banner\b|boot\b|aaa\b|"
    r"snmp-server\b|ntp\b|end\s*$)",
    re.IGNORECASE,
)


def _extract_interfaces_sectionwise(parsed: ParsedConfig, raw_text: str) -> None:
    """Populate ``parsed.interfaces`` by scanning interface stanzas as sections.

    For each ``interface <name>`` line, subsequent lines are fed to
    ``_consume_iface_line`` until a blank line, a ``!`` separator, or a new
    top-level stanza is reached. This is independent of indentation, so it works
    for both normally-indented configs and flattened dumps.
    """
    lines = raw_text.splitlines()
    n = len(lines)
    i = 0
    while i < n:
        s = lines[i].strip()
        # Capture the FULL interface name, including any space between the type
        # token and number (e.g. "Ethernet 0/1", "Vlan 1", "Loopback 99").
        m = re.match(r"^interface\s+(.+\S)\s*$", s)
        if not m:
            i += 1
            continue
        name = m.group(1)
        # Skip "interface range ..." group commands — they are bulk editors, not
        # a single addressable port, and never carry their own IP.
        if re.match(r"^range\b", name, re.IGNORECASE):
            i += 1
            continue
        iface = ParsedInterface(name=name, kind=_iface_kind(name))
        i += 1
        while i < n:
            cs = lines[i].strip()
            if cs == "" or cs == "!":
                i += 1
                break
            if _SECTION_BOUNDARY_RE.match(cs):
                break  # start of a new top-level stanza; do not advance
            _consume_iface_line(cs, iface)  # consume if recognised, else ignore
            i += 1
        parsed.interfaces[name] = iface


_LINUX_IP_ADD_RE = re.compile(
    r"^ip\s+addr(?:ess)?\s+add\s+(\d+\.\d+\.\d+\.\d+/\d+)\s+dev\s+(\S+)",
    re.IGNORECASE,
)


def _extract_linux_host_ips(parsed: ParsedConfig, raw_text: str) -> None:
    """Recover IPs from Linux-host boot scripts (``ip addr add <cidr> dev <nic>``).

    The matched NIC (eth0/ens3/...) is recorded as a routed physical interface so
    the endpoint's address flows into ip_address_bulk just like a router port.
    Only the explicit host address is taken; ``ip route ... via <gw>`` lines are
    ignored so the default gateway is never mistaken for the host's own IP.
    """
    for line in raw_text.splitlines():
        m = _LINUX_IP_ADD_RE.match(line.strip())
        if not m:
            continue
        addr = _parse_ip_cidr(m.group(1))
        if addr is None:
            continue
        dev = m.group(2)
        iface = parsed.interfaces.get(dev)
        if iface is None:
            iface = ParsedInterface(name=dev, kind=_iface_kind(dev))
            parsed.interfaces[dev] = iface
        if iface.kind == "unknown":
            iface.kind = "physical"   # ens3/enp0s2/... normalise to Ethernet N
        iface.no_switchport_seen = True  # force is_routed() so the IP is emitted
        iface.ipv4.append(addr)


# ---------------------------------------------------------------------------
# Routing summary extraction (BGP / OSPF / EVPN / NVE / HSRP / PIM / etc.)
# ---------------------------------------------------------------------------

_ROUTING_BLOCK_RE = re.compile(
    r"^(router\s+(?:bgp|ospf|ospfv3|eigrp|isis)\s+\S+"
    r"|router\s+ospf\s+\d+\s+vrf\s+\S+"
    r"|interface\s+nve\d+"
    r"|evpn esi multihoming"
    r"|fabric forwarding anycast-gateway-mac"
    r"|ip pim rp-address"
    r"|hsrp"
    r"|vrrp"
    r"|track \d+"
    r")",
    re.IGNORECASE,
)


def _collect_routing_summary(parsed: ParsedConfig, raw_text: str) -> None:
    out: List[str] = []
    lines = raw_text.splitlines()
    n = len(lines)
    i = 0
    while i < n:
        line = lines[i]
        stripped = line.strip()
        if _ROUTING_BLOCK_RE.match(stripped):
            out.append(line.rstrip())
            i += 1
            while i < n and lines[i].startswith((" ", "\t")):
                out.append(lines[i].rstrip())
                i += 1
            out.append("")
            continue
        i += 1
    parsed.routing_summary_lines = out[:2000]  # bound the attribute string

    # Common top-level routing one-liners that aren't in a block.
    for line in lines:
        s = line.strip()
        if s.startswith(("ip pim rp-address", "ip nat ", "ip dhcp ")):
            parsed.routing_summary_lines.append(s)


# ---------------------------------------------------------------------------
# Bulk helpers
# ---------------------------------------------------------------------------

def parse_all(running_configs: Dict[str, str]) -> Dict[str, ParsedConfig]:
    """Parse a {label: raw_text} dict into {label: ParsedConfig}."""
    return {label: parse_running_config(raw, hostname_hint=label) for label, raw in running_configs.items()}
