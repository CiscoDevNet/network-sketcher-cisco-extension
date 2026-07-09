# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""Parse a single Cisco running-config (IOS / IOS-XE / NX-OS / IOS-XR /
ASA(FTD/FDM) — all six OS families are IN-SCOPE for Phase 1, see DESIGN.md
section 4.2.1, decision 7) text blob into structured data. FTD (whether
managed by FMC or locally by FDM) is parsed with the exact same logic path
as ASA: this module treats ASA/FTD/FDM as a SINGLE unified OS family
internally (``os_family == "asa"``, see ``ParsedConfig.os_family`` below) and
displayed to the user as **"ASA(FTD/FDM)"** (``stencil_mapper.
OS_FAMILY_DISPLAY``), since all three surface the exact same LINA/ASA-syntax
``show running-config`` text (confirmed via Cisco documentation research —
see ``config_converter/DESIGN.md`` section 4.2.1 for the citations and
confidence scores). No FMC/FDM REST API access is used at conversion time —
only the plain-text ASA-syntax export is targeted.

**Phase 1a/1b/1c/1d status (DESIGN.md section 6 roadmap)**: this module is a
WORKING parser for all Phase-1-scope OS families — **IOS, IOS-XE, NX-OS,
IOS-XR, and the unified ASA(FTD/FDM) family** (FTD/FDM via the exact same
code path as ASA, per this module's own docstring above — only their
ASA-syntax text export is targeted). This mirrors
``cml_converter/src/config_parser.py``'s own tolerant,
never-raise design: an OS-specific quirk this module does not yet know about
simply under-extracts rather than raising.

**Phase 1b (NX-OS) additions**: NX-OS's nested ``hsrp <group>`` / ``ip
<addr>`` sub-block (as opposed to IOS/IOS-XE's single-line ``standby <group>
ip <addr>``) is resolved by ``_extract_interfaces_sectionwise()``'s
``pending_hsrp_group`` state machine; NX-OS's bare ``ip access-list <name>``
(no ``standard``/``extended`` keyword) and its sequence-numbered ACL rule
lines (``10 permit ...``) are handled by ``_extract_acls()``; NX-OS's
``policy-map type qos <name>`` form is handled by ``_scan_bandwidth_limited_
policies()``'s ``_POLICY_MAP_RE``. All three additions are inert no-ops for
IOS/IOS-XE input (they only fire on syntax those platforms never emit).

**Phase 1c (IOS-XR) additions**: ``Bundle-Ether<n>`` (LAG) and
``MgmtEth<n>/.../.../...`` interface-kind recognition; ``ipv4 address``/
``ipv4 access-group <name> ingress|egress``/``bundle id <n> mode <mode>``/a
bare per-interface ``vrf <name>`` child-line in ``_consume_iface_line()``;
``ipv4 access-list <name>`` (also sequence-numbered like NX-OS) in
``_extract_acls()``; the commit-model's nested ``neighbor <ip>`` /
``remote-as <asn>`` two-line BGP peer form (vs. classic IOS's single-line
form) via ``_extract_bgp()``'s ``pending_neighbor_ip`` state machine.

**Phase 1d (ASA/FTD/FDM, unified) additions**: ``nameif <zone>`` /
``security-level <n>`` interface child-lines (``ParsedInterface.nameif``); ASA's named,
single-line ``access-list <name> extended permit|deny ...`` ACL form in
``_extract_acls()``; ASA's GLOBAL (not per-interface-nested) ``access-group
<acl> in|out interface <nameif>`` command, resolved against each
interface's ``nameif`` by ``_apply_asa_access_groups()`` (run AFTER
interface extraction); ``object network``/``object-group network <name>``
existence-only collection (``ParsedConfig.nat_objects``) by
``_extract_asa_nat_objects()`` — full NAT/object-model resolution remains
out of scope per DESIGN.md 4.2.1.

Ported, near-verbatim, from ``cml_converter/src/config_parser.py`` (the
indentation-agnostic interface-stanza scanner, the CiscoConfParse-optional
hostname/vlan/vrf pass, and the per-line interface attribute consumer).
``_extract_linux_host_ips()`` was intentionally NOT ported — it is CML-lab
specific (Linux "desktop"/"alpine"/"ubuntu" nodes booted from a shell
script) and has no equivalent in real Cisco running-configs.

NEW, config_converter-specific, extraction added on top of the cml_converter
base (DESIGN.md section 4.2, items 3-6):

  * HSRP/VRRP/GLBP virtual IP extraction, kept SEPARATE from
    ``ParsedInterface.ipv4`` (DESIGN.md section 4.3's subnet-matching
    algorithm must exclude virtual IPs from its candidate pool).
  * ACL definitions (``ip access-list`` / numbered ``access-list``) and their
    application to an interface (``ip access-group <name> in|out``), kept as
    an ordered list of (action, raw) so evaluation order is preserved —
    required by requirement G's deny-all detection (DESIGN.md section 4.7).
  * ``ip nat inside`` / ``ip nat outside``, ``ip address dhcp``/``negotiated``,
    ``crypto map`` application — requirement H's WAN-scoring signals
    (DESIGN.md section 4.8).
  * A light, best-effort ``router bgp <asn>`` / ``neighbor <ip> remote-as
    <asn>`` extraction (external-AS detection only, NOT a full BGP config
    model) — requirement H signal 7 (DESIGN.md section 4.8).
  * A light, best-effort bandwidth-limit detection: a direct ``traffic-shape
    rate`` command on the interface, OR a ``service-policy`` reference to a
    ``policy-map`` whose body contains ``shape average`` / ``police`` /
    ``priority`` — requirement H signal 9 / ``bandwidth_limit_configured``
    (DESIGN.md section 4.8 / 8.3 decision 8). This is intentionally a
    best-effort MQC-name-resolution scan, NOT a full QoS policy model.

OS-specific parsing for IOS-XR/ASA(FTD/FDM) (DESIGN.md section 4.2.1,
decision 7) is DEFERRED to Phase 1c/1d (DESIGN.md section 6) — see the
module-level TODO markers below for exactly what each follow-up phase must
add.

Optional dependency: ``ciscoconfparse2`` (see ``requirements.txt`` and
DESIGN.md section 4.2.2, decision 10) — the same soft-dependency pattern as
``cml_converter``'s ``_HAVE_CCP`` flag: use it when installed to improve
hostname/vlan/vrf extraction accuracy, but this module keeps working via its
own regex fallback when it is not installed. CiscoConfParse is deliberately
NOT used for interface/ACL/BGP/QoS extraction — the indentation-agnostic
section scanners below handle those uniformly regardless of whether CCP is
present, so those code paths are exercised identically either way.
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
    address: str
    prefix: int

    @property
    def cidr(self) -> str:
        return f"{self.address}/{self.prefix}"


@dataclass
class VirtualIPv4:
    """A first-hop-redundancy-protocol virtual/group address (HSRP/VRRP/GLBP).

    Deliberately NOT part of ``ParsedInterface.ipv4`` — DESIGN.md section 4.3
    requires these to be excluded from the same-subnet matching candidate
    pool, since a virtual IP does not belong to a single physical interface
    and would otherwise inflate a subnet's apparent candidate count.
    """
    protocol: str            # 'hsrp' | 'vrrp' | 'glbp'
    group: str
    address: str


@dataclass
class AclRule:
    """One ordered ACL statement. Order MUST be preserved for requirement G's
    deny-all evaluation (DESIGN.md section 4.7) — a `deny ip any any` that is
    preceded by any `permit` statement does NOT make the interface closed."""
    action: str               # 'permit' | 'deny'
    raw: str                  # original statement text (audit / debug)


@dataclass
class AclDefinition:
    name: str
    rules: List[AclRule] = field(default_factory=list)

    def is_bidirectional_deny_all(self) -> bool:
        """DESIGN.md 4.7: an ACL amounts to an unconditional deny of all
        traffic if and only if it contains at least one rule and NONE of its
        explicit rules is a ``permit``. This is a safe/sufficient condition
        (not just necessary) because every Cisco ACL ends with an IMPLICIT
        ``deny ip any any`` after its last explicit statement — so as long as
        no explicit ``permit`` exists anywhere in the list, literally no
        packet can ever match a permit (whether via an explicit deny or via
        the implicit trailing deny), regardless of how specific/narrow the
        explicit deny statements are.

        Conservative by construction: an ACL with zero rules (e.g. referenced
        by name but never actually defined in the config we saw) returns
        False rather than True, per DESIGN.md 4.7's "when in doubt, False"
        guidance — an empty/undefined ACL applied to an interface has no
        confirmed blocking effect we can point to.
        """
        if not self.rules:
            return False
        return all(rule.action == "deny" for rule in self.rules)


@dataclass
class ParsedInterface:
    name: str                          # e.g. "GigabitEthernet0/1", "Vlan10", "Loopback0", "Port-channel1"
    kind: str                          # 'physical' | 'svi' | 'loopback' | 'subif' | 'portchannel' | 'mgmt' | 'tunnel' | 'unknown'
    description: Optional[str] = None
    shutdown: bool = False
    switchport: bool = True            # IOS default is L2 on switch platforms; set True only when seen
    no_switchport_seen: bool = False
    mode: Optional[str] = None         # 'access' | 'trunk' | None
    access_vlan: Optional[int] = None
    trunk_native_vlan: Optional[int] = None
    trunk_allowed_vlans: List[int] = field(default_factory=list)
    ipv4: List[IPv4Addr] = field(default_factory=list)
    ipv4_secondary: List[IPv4Addr] = field(default_factory=list)
    ipv4_dhcp: bool = False            # 'ip address dhcp' / 'negotiated' — WAN signal (DESIGN.md 4.8)
    virtual_ips: List[VirtualIPv4] = field(default_factory=list)  # HSRP/VRRP/GLBP (DESIGN.md 4.3)
    vrf: Optional[str] = None
    channel_group: Optional[int] = None
    channel_mode: Optional[str] = None  # 'active' | 'passive' | 'on' | etc
    vpc_peer_link: bool = False        # NX-OS 'vpc peer-link' on a Port-channel (DESIGN.md 4.9 same-batch pairing)
    vpc_id: Optional[int] = None       # NX-OS 'vpc <n>' (non-peer-link, e.g. downstream vPC to a server/access device)
    mtu: Optional[int] = None
    speed: Optional[str] = None
    ospf_area: Optional[str] = None
    nat_side: Optional[str] = None     # 'inside' | 'outside' | None — WAN signal (DESIGN.md 4.8)
    crypto_map: Optional[str] = None   # WAN/VPN signal (DESIGN.md 4.8)
    acl_in: Optional[str] = None       # ACL name applied inbound (DESIGN.md 4.7)
    acl_out: Optional[str] = None      # ACL name applied outbound (DESIGN.md 4.7)
    bandwidth_limit_configured: bool = False  # shape/police/priority/MQC QoS — NEW WAN signal 9 (DESIGN.md 4.8, 8.3 decision 8)
    # NOTE (bandwidth-semantics investigation, DESIGN.md 4.8 signal 9):
    # ``bandwidth_limit_configured`` is deliberately set ONLY from actual
    # traffic-enforcement constructs (QoS `shape average`/`police`/`priority`
    # inside a `service-policy`-referenced policy-map, or a direct
    # `traffic-shape rate` command — see `_consume_iface_line()` and
    # `_scan_bandwidth_limited_policies()` below). The plain interface-level
    # `bandwidth <kbps>` command is INTENTIONALLY NEVER read into this field
    # (or any other field) — it is a very common Cisco config-review gotcha:
    # `bandwidth <kbps>` only feeds IGP metric calculations (OSPF cost,
    # EIGRP/IS-IS metric) and interface-utilisation-percentage displays, it
    # does NOT enforce any actual rate limit on the wire. Conflating it with
    # `bandwidth_limit_configured` would falsely imply an interface is
    # traffic-shaped merely because an operator set a routing-metric hint (or
    # left an IOS default, e.g. 1544 kbps on a Serial interface). This field
    # therefore only ever reflects a genuine, enforced rate-limiting signal.
    nameif: Optional[str] = None       # ASA/FTD/FDM zone name (DESIGN.md 4.2.1) — 'nameif outside' is a strong WAN signal (DESIGN.md 4.8)

    def is_routed(self) -> bool:
        """Ported from cml_converter/src/config_parser.py."""
        if self.kind in {"svi", "loopback", "mgmt", "tunnel"}:
            return True
        if self.kind in {"subif"}:
            return True
        if self.kind in {"portchannel", "physical"}:
            return self.no_switchport_seen or bool(self.ipv4)
        return bool(self.ipv4)


@dataclass
class BgpPeer:
    """Best-effort external-AS BGP neighbour (DESIGN.md 4.8 signal 7)."""
    neighbor_ip: str
    remote_as: str


@dataclass
class ParsedConfig:
    hostname: Optional[str] = None
    # 'nxos' | 'ios' | 'iosxe' | 'iosxr' | 'asa' | 'unknown' (all in Phase 1
    # scope, DESIGN.md 4.2.1). NOTE: 'asa' is a UNIFIED internal discriminator
    # for ASA, FTD, and FDM-managed FTD alike -- detect_os_family() below
    # never returns a separate 'ftd' value (there is no reliable way to tell
    # them apart from the ASA-syntax text alone, and Cisco documentation
    # confirms all three emit the same LINA/ASA CLI syntax for
    # 'show running-config', see DESIGN.md 4.2.1). The user-facing display
    # string for this family is "ASA(FTD/FDM)" (stencil_mapper.
    # OS_FAMILY_DISPLAY), not a separate "FTD"/"FDM" label.
    os_family: str = "unknown"
    local_asn: Optional[str] = None
    bgp_peers: List[BgpPeer] = field(default_factory=list)
    vlans: Dict[int, str] = field(default_factory=dict)
    vrfs: Set[str] = field(default_factory=set)
    vpc_domain: Optional[int] = None   # NX-OS 'vpc domain <n>' (DESIGN.md 4.9 same-batch vPC-peer-link pairing scope)
    interfaces: Dict[str, ParsedInterface] = field(default_factory=dict)
    acls: Dict[str, AclDefinition] = field(default_factory=dict)
    nat_objects: Set[str] = field(default_factory=set)  # ASA/FTD 'object [network|network-group]' names seen (existence only, DESIGN.md 4.2.1) — NAT/H-signal support
    routing_summary_lines: List[str] = field(default_factory=list)
    raw_size_bytes: int = 0
    fall_through_count: int = 0
    parsed_line_count: int = 0
    source_filename: Optional[str] = None  # config_converter-specific: original file path, for the report


# ---------------------------------------------------------------------------
# Helpers (ported from cml_converter/src/config_parser.py)
# ---------------------------------------------------------------------------

# Interface-kind classification. A type token must be followed by a digit
# (``(?=\s*\d)``) so we never match a bare keyword; physical types are
# matched WITH OR WITHOUT a slash. The ``(?=\s*\d)`` lookahead tolerates an
# optional space between the type token and the interface number — IOS
# accepts (and some dumps emit) both "Ethernet0/1" and "Ethernet 0/1" /
# "Vlan 1" / "Loopback 99". "<Type><n>/<n>/<n>/<n>" 4-slash IOS-XR physical
# naming (e.g. "GigabitEthernet0/0/0/0") needs no separate pattern — the
# existing physical-type tokens already match regardless of how many
# "/"-separated numbers follow.
#
# Phase 1c (IOS-XR, DESIGN.md 4.2.1) additions: "Bundle-Ether<n>" (LAG,
# IOS-XR's Port-channel equivalent) in the portchannel alternation, and
# "MgmtEth<n>/.../.../..." in the mgmt alternation (IOS-XR's management
# interface is named "MgmtEth", not bare "mgmt"/"management").
_IFACE_KIND_RE = [
    (re.compile(r"^vl(?:an)?(?=\s*\d)", re.IGNORECASE), "svi"),
    (re.compile(r"^(?:loopback|loop|lo)(?=\s*\d)", re.IGNORECASE), "loopback"),
    (re.compile(r"^(?:management|mgmt|mgmteth)(?=\s*\d)", re.IGNORECASE), "mgmt"),
    (re.compile(r"^tun(?:nel)?(?=\s*\d)", re.IGNORECASE), "tunnel"),
    (re.compile(r"^nve(?=\s*\d)", re.IGNORECASE), "tunnel"),
    (re.compile(r"^(?:port-?channel|po|bundle-?ether|be)(?=\s*\d)", re.IGNORECASE), "portchannel"),
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
    # Forms supported (trailing tokens such as "standby x.x.x.x" are ignored
    # — we always take the leading address + mask/prefix):
    #   "10.0.0.1/24"                              (NX-OS)
    #   "10.0.0.1 255.255.255.0"                   (IOS / IOS-XE)
    #   "10.0.0.1 255.255.255.0 standby 10.0.0.2"  (secondary/HSRP tail)
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


# Sentinels: top-level (indent 0) lines we deliberately ignore (counted as
# parsed but no action). Extended beyond cml_converter's list with the
# top-level keywords consumed by config_converter's own dedicated block
# scanners below (ACL / BGP / QoS policy-map) so they are not double-counted
# as "fall-through" even though a separate pass does extract them.
_IGNORED_PREFIXES = (
    "!", "#", "version ", "boot ", "service ", "no service ", "no ip ", "ip ",
    "ipv6 ", "no ipv6 ", "logging ", "no logging ", "username ", "password ",
    "snmp-server ", "rmon ", "no password", "control-plane", "line ",
    "exec-timeout", "transport ", "stopbits", "no exec", "domain-lookup",
    "domain-name", "name-server", "spanning-tree", "no spanning-tree",
    "policy-map", "class-map", "class ", "policy-template", "system ",
    "feature ", "no feature ", "errdisable ", "ntp ", "clock ", "logfile",
    "session-limit", "license", "macro ", "vstack", "diagnostic ", "archive",
    "memory ", "no memory ", "no aaa", "aaa ", "redundancy", "service-policy",
    "router ", "neighbor ", "network ", "bgp ", "access-list ",
    # Phase 1c (IOS-XR, DESIGN.md 4.2.1): IOS-XR-only top-level directives
    # that carry no C/D/E/F/G/H signal for this tool (route-policy/prefix-
    # set/community-set bodies are handled as opaque skipped blocks via
    # _SECTION_BOUNDARY_RE above; "ipv4 "/"no ipv4 " mirrors the existing
    # "ip "/"no ip " catch-all since IOS-XR spells many commands "ipv4 ..."
    # instead of "ip ...").
    "ipv4 ", "no ipv4 ", "route-policy ", "prefix-set ", "community-set ",
    "commit", "call-home", "telemetry ", "grpc",
    # Phase 1d (ASA/FTD, DESIGN.md 4.2.1): ASA-only top-level directives with
    # no direct config_converter signal beyond what _apply_asa_access_groups()/
    # _extract_asa_nat_objects() already extract separately.
    "object ", "object-group ", "nat ", "same-security-traffic ", "access-group ",
    "mtu ", "icmp ", "sysopt ", "asdm ", "http ", "telnet ", "ssh ", "management-access ",
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
# OS family detection
# ---------------------------------------------------------------------------

def detect_os_family(raw_text: str) -> str:
    """Heuristic OS family detection from a single running-config blob.

    Extends cml_converter's original 4-way (nxos/iosxe/iosxr/ios) heuristic
    with ASA(FTD/FDM) detection and a more reliable IOS-vs-IOS-XE signal: the
    ``version`` train number itself (12.x/15.x = classic IOS, 16.x/17.x =
    IOS-XE "Everest"/"Fuji"/... trains on the Denali+ unified image) rather
    than requiring the literal substring "ios" to appear somewhere in the
    banner — real ``show running-config`` output does not reliably contain
    that substring, so the original substring check under-detected IOS as a
    fallback "iosxe" guess.

    SCOPE DECISION (out of scope, EoL — no code change made here): legacy
    IOS-XE 3.x (pre-"Denali" unification, e.g. first-generation ASR1000/
    ISR4000 platforms) self-reports as ``version 15.x`` in its
    ``show running-config`` output -- indistinguishable, by this function's
    ``version`` heuristic alone, from classic IOS. Cisco declared the
    pre-unification IOS-XE 3.x train End-of-Life, so this tool intentionally
    does NOT add special-casing to disambiguate it: such input is
    misclassified as ``"ios"`` rather than rejected outright. See
    `config_converter/README.md`'s "Supported OS versions" section and
    `DESIGN.md` section 4.2.1 for the user-facing documentation of this known,
    accepted limitation.

    ASA(FTD/FDM) unification: FTD's (whether managed by FMC or locally by
    FDM) "show running-config"-equivalent text export is NOT reliably
    distinguishable from classic ASA from the banner alone (both can start
    with "ASA Version" / ": Saved"), and Cisco documentation confirms all
    three (ASA, FMC-managed FTD, FDM-managed FTD) emit the exact same
    LINA/ASA CLI syntax for this command (DESIGN.md 4.2.1 cites the specific
    sources/confidence scores). Rather than inventing a separate, never-
    reachable ``"ftd"`` return value, this function returns the single
    unified ``"asa"`` discriminator for all three -- the identical parsing
    logic path is used for all of them regardless (per this module's
    docstring), and the user-facing display string is "ASA(FTD/FDM)"
    (``stencil_mapper.OS_FAMILY_DISPLAY``).
    """
    head = raw_text[:2000]
    head_lower = head.lower()

    if "!! ios xr configuration" in head_lower or re.search(r"^\s*rp/\d+", head, re.MULTILINE):
        return "iosxr"
    if head_lower.lstrip().startswith(": saved") or "asa version" in head_lower:
        return "asa"
    # NOTE: a 4th clause -- `"feature ospf" in head_lower and "nxos" in
    # head_lower` -- was removed here (dead-code cleanup). Real NX-OS
    # `show running-config` output does not reliably contain the literal
    # substring "nxos" anywhere in its header (confirmed against the bundled
    # `Input_data/sample1/nxos_core01.txt` sample, which has no "nxos" substring in
    # its first 2000 characters and is already correctly classified by the
    # `!command: show running-config` boundary-marker regex below); the only
    # realistic way this clause could ever fire is a coincidental hostname
    # containing "nxos" (e.g. this repo's own `NXOS-CORE01` sample) combined
    # with an OSPF feature line, which is a naming coincidence, not a
    # genuine NX-OS syntax signal, and is unreachable in practice because the
    # boundary-marker clause above already classifies every bundled/realistic
    # NX-OS sample first. The three remaining clauses below (`vdc `,
    # `feature nv overlay`, and the `!command: show running-config` boundary
    # marker) are sufficient, reliable, NX-OS-specific signals on their own.
    if (
        "vdc " in head_lower
        or "feature nv overlay" in head_lower
        or re.search(r"^!command:\s*show running-config", head_lower, re.MULTILINE)
    ):
        return "nxos"

    m = re.search(r"^version\s+(\d+)\.", head, re.MULTILINE | re.IGNORECASE)
    if m:
        major = int(m.group(1))
        if major >= 16:
            return "iosxe"
        if major in (12, 15):
            return "ios"

    # Best-effort fallbacks when the version line is missing/ambiguous.
    if "platform " in head_lower or "interface gigabitethernet0/0" in head_lower:
        return "iosxe"
    if "feature " in head_lower:
        return "nxos"
    return "ios"


# ---------------------------------------------------------------------------
# Main parse function
# ---------------------------------------------------------------------------

def parse_running_config(raw_text: str, hostname_hint: Optional[str] = None) -> ParsedConfig:
    """Parse a single device running-config into a ParsedConfig.

    Never raises on a single malformed line — matches cml_converter's
    documented tolerant-parsing design goal. IOS/IOS-XE only for Phase 1a;
    other OS families are OS-detected but run through the same generic
    scanners (best-effort, likely under-extracting) until their Phase
    1b/1c/1d follow-up lands (DESIGN.md section 6).
    """
    parsed = ParsedConfig(hostname=hostname_hint, raw_size_bytes=len(raw_text or ""))
    if not raw_text:
        return parsed

    parsed.os_family = detect_os_family(raw_text)

    if _HAVE_CCP:
        try:
            if parsed.os_family == "nxos":
                ccp_syntax = "nxos"
            elif parsed.os_family == "iosxr":
                ccp_syntax = "iosxr"  # Phase 1c; falls back to regex below if unsupported by the installed version
            else:
                ccp_syntax = "ios"
            ccp = CiscoConfParse(raw_text.splitlines(), syntax=ccp_syntax)
            _parse_with_ccp(parsed, ccp)
        except Exception:
            # Never let an optional-dependency quirk abort parsing — fall
            # back to the always-available regex pass instead.
            _parse_with_regex(parsed, raw_text)
    else:
        _parse_with_regex(parsed, raw_text)

    # Interface stanzas are extracted with an indentation-agnostic section
    # scan regardless of the CCP/regex path above (see
    # _extract_interfaces_sectionwise) — this is the primary source of truth
    # for interface attributes.
    policy_bandwidth_limited = _scan_bandwidth_limited_policies(raw_text)
    _extract_interfaces_sectionwise(parsed, raw_text, policy_bandwidth_limited)

    # config_converter-specific extensions beyond cml_converter (DESIGN.md
    # 4.2 items 3-6): ACL definitions and best-effort external-AS BGP peers.
    _extract_acls(parsed, raw_text)
    _extract_bgp(parsed, raw_text)

    # Phase 1d (ASA/FTD, DESIGN.md 4.2.1): ASA applies an ACL to a zone via a
    # GLOBAL "access-group <acl> in|out interface <nameif>" command (NOT a
    # nested interface child-line like IOS's "ip access-group"), so it must
    # be resolved against the nameif values collected by
    # _extract_interfaces_sectionwise() above. Also collects
    # "object [network|-group network] <name>" existence (NAT/H-signal,
    # DESIGN.md 4.8) -- both are no-ops for non-ASA/FTD input.
    _apply_asa_access_groups(parsed, raw_text)
    _extract_asa_nat_objects(parsed, raw_text)

    # Collect routing summary (BGP/OSPF/EVPN/NVE/HSRP/PIM/EIGRP/ISIS sections).
    _collect_routing_summary(parsed, raw_text)

    # config_converter extension (DESIGN.md 4.9): NX-OS vPC domain id, used
    # to SCOPE same-batch vPC-peer-link pairing in topology_mapper.py's
    # synthesize_portchannel_member_links() -- two devices' orphaned 'vpc
    # peer-link' Port-channels are only paired directly when they share the
    # same domain id (or neither side reports one), so an input batch that
    # happens to contain MULTIPLE independent vPC pairs is not incorrectly
    # cross-wired.
    _extract_vpc_domain(parsed, raw_text)
    return parsed


# ---------------------------------------------------------------------------
# CiscoConfParse-based path (hostname / vlan / vrf only — see module
# docstring for why interface/ACL/BGP/QoS extraction always uses the
# indentation-agnostic regex scanners regardless of CCP availability)
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

    # VRF: NX-OS 'vrf context X'; IOS-XE 'vrf definition X'; IOS legacy
    # 'ip vrf X'; IOS-XR (Phase 1c) bare top-level 'vrf X'.
    for vrf_obj in ccp.find_objects(r"^vrf\s+(?:context|definition)\s+\S+|^ip\s+vrf\s+\S+|^vrf\s+\S+"):
        parts = vrf_obj.text.strip().split()
        if parts and parts[-1] != "":
            parsed.vrfs.add(parts[-1])

    parsed.parsed_line_count = len(ccp.objs) if hasattr(ccp, "objs") else 0


# ---------------------------------------------------------------------------
# Regex fallback (used only if ciscoconfparse2 is unavailable)
# ---------------------------------------------------------------------------

def _parse_with_regex(parsed: ParsedConfig, raw_text: str) -> None:
    current_vlan_block: Optional[List[int]] = None

    for line in raw_text.splitlines():
        raw = line.rstrip()
        if not raw:
            current_vlan_block = None
            continue
        stripped = raw.lstrip()
        indent = len(raw) - len(stripped)

        if indent == 0:
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
            m = re.match(r"^(?:vrf\s+(?:context|definition)|ip\s+vrf|vrf)\s+(\S+)", stripped)
            if m:
                parsed.vrfs.add(m.group(1))
                parsed.parsed_line_count += 1
                continue
            if stripped.startswith("interface "):
                # Interfaces are fully handled by _extract_interfaces_sectionwise;
                # just count the line here so it is not miscounted as fall-through.
                parsed.parsed_line_count += 1
                continue
            if _line_is_ignored(stripped):
                parsed.parsed_line_count += 1
                continue
            parsed.fall_through_count += 1
        else:
            if current_vlan_block is not None:
                m = re.match(r"^name\s+(.+)$", stripped)
                if m:
                    for vid in current_vlan_block:
                        parsed.vlans[vid] = m.group(1).strip()
                parsed.parsed_line_count += 1
            elif _line_is_ignored(stripped):
                parsed.parsed_line_count += 1
            else:
                parsed.fall_through_count += 1


# ---------------------------------------------------------------------------
# Interface child-line consumer (shared by both paths)
# ---------------------------------------------------------------------------

def _consume_iface_line(stripped: str, iface: ParsedInterface, policy_bandwidth_limited: Set[str]) -> bool:
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

    # Phase 1d (ASA/FTD, DESIGN.md 4.2.1): the interface's firewall zone name.
    # A strong WAN signal when its value is "outside" (DESIGN.md 4.8).
    m = re.match(r"^nameif\s+(\S+)$", stripped, re.IGNORECASE)
    if m:
        iface.nameif = m.group(1)
        return True
    if re.match(r"^security-level\s+\d+$", stripped, re.IGNORECASE):
        return True  # recognised, no config_converter signal derived from it (yet)

    m = re.match(r"^vrf\s+(?:member|forwarding)\s+(\S+)$", stripped)
    if m:
        iface.vrf = m.group(1)
        return True
    # Phase 1c (IOS-XR, DESIGN.md 4.2.1): IOS-XR assigns an interface's VRF
    # with a bare "vrf <name>" child line (no "member"/"forwarding" keyword).
    # Safe to check unconditionally here since this function only ever sees
    # lines already known to be inside an interface stanza.
    m = re.match(r"^vrf\s+(\S+)$", stripped, re.IGNORECASE)
    if m:
        iface.vrf = m.group(1)
        return True

    # config_converter extension: DHCP/negotiated address (WAN signal, DESIGN.md
    # 4.8) — must be checked BEFORE the generic "ip address <cidr>" match below.
    # Phase 1c: IOS-XR uses "ipv4 address ..." instead of "ip address ...".
    if re.match(r"^(?:ip|ipv4)\s+address\s+(?:dhcp|negotiated)\s*$", stripped, re.IGNORECASE):
        iface.ipv4_dhcp = True
        return True

    m = re.match(r"^(?:ip|ipv4)\s+address\s+(.+?)(?:\s+secondary)?$", stripped, re.IGNORECASE)
    if m:
        addr = _parse_ip_cidr(m.group(1))
        if addr:
            if stripped.lower().endswith(" secondary"):
                iface.ipv4_secondary.append(addr)
            else:
                iface.ipv4.append(addr)
            return True

    # config_converter extension: HSRP/VRRP/GLBP virtual IP (DESIGN.md 4.3).
    m = re.match(r"^(standby|vrrp|glbp)\s+(\d+)\s+ip\s+(\d+\.\d+\.\d+\.\d+)", stripped, re.IGNORECASE)
    if m:
        protocol = {"standby": "hsrp", "vrrp": "vrrp", "glbp": "glbp"}[m.group(1).lower()]
        iface.virtual_ips.append(VirtualIPv4(protocol=protocol, group=m.group(2), address=m.group(3)))
        return True

    # config_converter extension: NAT side (DESIGN.md 4.8 signal 3).
    m = re.match(r"^ip\s+nat\s+(inside|outside)$", stripped, re.IGNORECASE)
    if m:
        iface.nat_side = m.group(1).lower()
        return True

    # config_converter extension: crypto map / VPN (DESIGN.md 4.8 signal 6).
    m = re.match(r"^crypto\s+map\s+(\S+)", stripped, re.IGNORECASE)
    if m:
        iface.crypto_map = m.group(1)
        return True

    # config_converter extension: ACL application (DESIGN.md 4.7). Phase 1c:
    # IOS-XR spells this "ipv4 access-group <name> ingress|egress" instead of
    # IOS/IOS-XE/NX-OS's "ip access-group <name> in|out".
    m = re.match(r"^(?:ip|ipv4)\s+access-group\s+(\S+)\s+(in|out|ingress|egress)$", stripped, re.IGNORECASE)
    if m:
        if m.group(2).lower() in ("in", "ingress"):
            iface.acl_in = m.group(1)
        else:
            iface.acl_out = m.group(1)
        return True

    # config_converter extension: bandwidth-limit signal (DESIGN.md 4.8 signal 9).
    m = re.match(r"^service-policy\s+(input|output)\s+(\S+)$", stripped, re.IGNORECASE)
    if m:
        if m.group(2) in policy_bandwidth_limited:
            iface.bandwidth_limit_configured = True
        return True
    if re.match(r"^traffic-shape\s+rate\s+\d+", stripped, re.IGNORECASE):
        iface.bandwidth_limit_configured = True
        return True

    m = re.match(r"^channel-group\s+(\d+)(?:\s+mode\s+(\S+))?$", stripped)
    if m:
        iface.channel_group = int(m.group(1))
        iface.channel_mode = m.group(2)
        return True
    # Phase 1c (IOS-XR, DESIGN.md 4.2.1): IOS-XR's LAG-membership equivalent
    # of "channel-group <n> mode <mode>" is "bundle id <n> mode <mode>".
    m = re.match(r"^bundle\s+id\s+(\d+)(?:\s+mode\s+(\S+))?$", stripped, re.IGNORECASE)
    if m:
        iface.channel_group = int(m.group(1))
        iface.channel_mode = m.group(2)
        return True

    # config_converter extension (DESIGN.md 4.9): NX-OS vPC signals on a
    # logical Port-channel. "vpc peer-link" marks the dedicated L2 trunk
    # directly between the two vPC peer switches themselves (no IP address)
    # -- the strongest, most reliable signal for same-batch device pairing.
    # A bare "vpc <n>" (no "peer-link") instead marks a DOWNSTREAM vPC member
    # port-channel toward an external device (e.g. a server or access
    # switch) that is legitimately NOT expected to be one of the other
    # configs in this batch, and must be kept distinct from "peer-link".
    if re.match(r"^vpc\s+peer-link$", stripped, re.IGNORECASE):
        iface.vpc_peer_link = True
        return True
    m = re.match(r"^vpc\s+(\d+)$", stripped, re.IGNORECASE)
    if m:
        iface.vpc_id = int(m.group(1))
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
    # "bandwidth " (bare interface-level bandwidth, e.g. "bandwidth 1544") is
    # explicitly listed here rather than falling through unrecognised: it is
    # deliberately recognised-but-discarded, NOT treated as a bandwidth-limit
    # signal (DESIGN.md 4.8 signal 9 investigation) -- see the extended
    # comment on ``ParsedInterface.bandwidth_limit_configured`` above for why
    # this IGP-metric-only command must never set that field.
    if stripped.startswith((
        "ip ospf ", "ip pim", "ip helper",
        "ipv6 ", "no ipv6 ",
        "storm-control", "media-type",
        "negotiation", "duplex", "load-interval", "lldp ",
        "logging event", "spanning-tree", "no spanning-tree", "bfd",
        "tx-queue-limit", "hold-queue",
        "carrier-delay", "platform ", "no platform ",
        "fabric forwarding", "no shutdown", "shutdown",
        "standby ", "vrrp ", "glbp ", "bandwidth ",
    )):
        return True

    return False


# ---------------------------------------------------------------------------
# Indentation-agnostic interface extraction (ported from cml_converter)
# ---------------------------------------------------------------------------

# A line that begins one of these top-level stanzas terminates the interface
# (or ACL / BGP / policy-map) block currently being collected — this lets us
# delimit stanzas by *content* rather than indentation, so configs whose
# child lines are flattened to column 0 still parse correctly. Only
# UNAMBIGUOUSLY top-level keywords belong here (see cml_converter's original
# comment on why bare "vrf"/"spanning-tree"/"monitor" must NOT be listed).
_SECTION_BOUNDARY_RE = re.compile(
    r"^(?:interface\b|router\b|line\b|vlan\b|"
    r"vrf\s+(?:context|definition)\b|ip\s+vrf\b|hostname\b|"
    r"control-plane\b|route-map\b|policy-map\b|class-map\b|"
    r"ip\s+access-list\b|ipv4\s+access-list\b|access-list\b|"
    r"crypto\b|banner\b|boot\b|aaa\b|"
    # Phase 1c (IOS-XR, DESIGN.md 4.2.1): these top-level IOS-XR stanzas must
    # not be mistaken for interface/ACL body content by the indentation-
    # agnostic scanners above.
    r"route-policy\b|prefix-set\b|community-set\b|"
    # Phase 1d (ASA/FTD, DESIGN.md 4.2.1): ASA's "object [network|-group
    # network] <name>" blocks must not be absorbed into a preceding
    # interface/ACL stanza scan.
    r"object\b|"
    r"snmp-server\b|ntp\b|end\s*$)",
    re.IGNORECASE,
)


# NX-OS HSRP uses a NESTED sub-block instead of IOS/IOS-XE's single-line
# ``standby <group> ip <addr>`` form (Phase 1b, DESIGN.md section 6):
#
#   interface Vlan10
#     hsrp 10
#       ip 10.10.10.1
#       priority 110
#
# The section-wise scanner below is intentionally indentation-agnostic (see
# the module docstring / cml_converter heritage), so it cannot rely on
# indentation depth to know that "ip 10.10.10.1" belongs to the "hsrp 10"
# line above it rather than being a new top-level interface attribute. We
# instead track a small piece of per-interface state: while the most
# recently consumed line was "hsrp <group>" (or one of its known child
# keywords), a bare "ip <addr>" line is resolved as that HSRP group's
# virtual IP. Any OTHER line ends the pending HSRP sub-block. This is
# inert for IOS/IOS-XE configs, which never emit a bare top-level "hsrp"
# keyword inside an interface stanza.
_NXOS_HSRP_GROUP_RE = re.compile(r"^hsrp\s+(\d+)\s*$", re.IGNORECASE)
_NXOS_HSRP_VIP_RE = re.compile(r"^ip\s+(\d+\.\d+\.\d+\.\d+)\s*$", re.IGNORECASE)
_NXOS_HSRP_CHILD_RE = re.compile(
    r"^(?:priority\b|preempt\b|timers\b|authentication\b|track\b)", re.IGNORECASE)


def _extract_interfaces_sectionwise(
    parsed: ParsedConfig, raw_text: str, policy_bandwidth_limited: Set[str]
) -> None:
    """Populate ``parsed.interfaces`` by scanning interface stanzas as sections.

    For each ``interface <name>`` line, subsequent lines are fed to
    ``_consume_iface_line`` until a blank line, a ``!`` separator, or a new
    top-level stanza is reached. This is independent of indentation, so it
    works for both normally-indented configs and flattened dumps.
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
        pending_hsrp_group: Optional[str] = None  # NX-OS nested "hsrp N" block (see above)
        while i < n:
            cs = lines[i].strip()
            if cs == "" or cs == "!":
                i += 1
                break
            if _SECTION_BOUNDARY_RE.match(cs):
                break  # start of a new top-level stanza; do not advance

            if pending_hsrp_group is not None:
                vip = _NXOS_HSRP_VIP_RE.match(cs)
                if vip:
                    iface.virtual_ips.append(
                        VirtualIPv4(protocol="hsrp", group=pending_hsrp_group, address=vip.group(1)))
                    i += 1
                    continue
                if _NXOS_HSRP_CHILD_RE.match(cs):
                    i += 1  # recognised HSRP child keyword, no new info to capture
                    continue
                pending_hsrp_group = None  # any other line ends the nested sub-block

            grp = _NXOS_HSRP_GROUP_RE.match(cs)
            if grp:
                pending_hsrp_group = grp.group(1)
                i += 1
                continue

            _consume_iface_line(cs, iface, policy_bandwidth_limited)  # consume if recognised, else ignore
            i += 1
        parsed.interfaces[name] = iface


# ---------------------------------------------------------------------------
# ACL definitions (config_converter extension, DESIGN.md 4.7)
# ---------------------------------------------------------------------------

def _extract_acls(parsed: ParsedConfig, raw_text: str) -> None:
    """Scan ``ip access-list standard|extended <name>`` blocks and legacy
    numbered ``access-list <n> permit|deny ...`` one-liners into
    ``parsed.acls``, preserving statement order (required by
    ``AclDefinition.is_bidirectional_deny_all()``, DESIGN.md 4.7).

    Phase 1b (NX-OS, DESIGN.md section 6) extension: NX-OS ACLs omit the
    ``standard``/``extended`` type keyword entirely (``ip access-list
    <name>``) and prefix every rule with a sequence number (``10 permit ...``,
    ``20 deny ...``) used for later re-sequencing/editing — both are handled
    below without affecting the IOS/IOS-XE path (the type keyword group is
    now optional, and the sequence-number prefix is simply stripped before
    the permit/deny match).

    Phase 1c (IOS-XR, DESIGN.md section 6) extension: IOS-XR spells the
    keyword ``ipv4 access-list <name>`` (not ``ip access-list``) and also
    uses NX-OS-style sequence-numbered rules (``10 deny ipv4 any any``) — both
    are handled by the same ``(?:ip|ipv4)`` alternation and sequence-number
    stripping added for NX-OS above.
    """
    lines = raw_text.splitlines()
    n = len(lines)
    i = 0
    while i < n:
        s = lines[i].strip()

        m = re.match(r"^(?:ip|ipv4)\s+access-list\s+(?:(standard|extended)\s+)?(\S+)\s*$", s, re.IGNORECASE)
        if m:
            name = m.group(2)
            acl = parsed.acls.setdefault(name, AclDefinition(name=name))
            i += 1
            while i < n:
                cs = lines[i].strip()
                if cs == "" or cs == "!":
                    i += 1
                    break
                if _SECTION_BOUNDARY_RE.match(cs):
                    break
                # Strip an NX-OS leading sequence number ("10 permit ...") before
                # matching the action keyword.
                cs_body = re.sub(r"^\d+\s+", "", cs)
                m2 = re.match(r"^(permit|deny)\b(.*)$", cs_body, re.IGNORECASE)
                if m2:
                    acl.rules.append(AclRule(action=m2.group(1).lower(), raw=cs))
                # remark / other lines inside the ACL body are ignored (not a rule)
                i += 1
            continue

        m = re.match(r"^access-list\s+(\d+)\s+(permit|deny)\b(.*)$", s, re.IGNORECASE)
        if m:
            name = m.group(1)
            acl = parsed.acls.setdefault(name, AclDefinition(name=name))
            acl.rules.append(AclRule(action=m.group(2).lower(), raw=s))
            i += 1
            continue

        # Phase 1d (ASA/FTD, DESIGN.md 4.2.1): named, single-line ASA ACL
        # statements ("access-list <name> extended permit|deny ..."). Unlike
        # the numbered form above, the name is an arbitrary token (not
        # digits-only), and every statement repeats "extended" -- both
        # distinguish this form from the numbered-IOS-ACL pattern above so
        # the two never collide.
        m = re.match(r"^access-list\s+(\S+)\s+extended\s+(permit|deny)\b(.*)$", s, re.IGNORECASE)
        if m:
            name = m.group(1)
            acl = parsed.acls.setdefault(name, AclDefinition(name=name))
            acl.rules.append(AclRule(action=m.group(2).lower(), raw=s))
            i += 1
            continue

        i += 1


# ---------------------------------------------------------------------------
# ASA/FTD-specific extraction (Phase 1d, DESIGN.md 4.2.1): global
# access-group -> nameif resolution, and object/object-group existence scan
# ---------------------------------------------------------------------------

_ASA_ACCESS_GROUP_RE = re.compile(
    r"^access-group\s+(\S+)\s+(in|out)\s+interface\s+(\S+)$", re.IGNORECASE)
_ASA_OBJECT_RE = re.compile(
    r"^object(?:-group)?\s+network\s+(\S+)", re.IGNORECASE)


def _apply_asa_access_groups(parsed: ParsedConfig, raw_text: str) -> None:
    """Resolve ASA's global ``access-group <acl> in|out interface <nameif>``
    against the ``nameif`` values already collected on each
    ``ParsedInterface`` (by ``_consume_iface_line()`` during
    ``_extract_interfaces_sectionwise()``, which MUST run before this
    function). No-op for IOS/IOS-XE/NX-OS/IOS-XR input, which never emits
    this command (they apply ACLs with a per-interface ``ip access-group``
    child-line instead, already handled directly in ``_consume_iface_line``).
    """
    nameif_to_iface = {
        iface.nameif: iface for iface in parsed.interfaces.values() if iface.nameif
    }
    if not nameif_to_iface:
        return
    for line in raw_text.splitlines():
        m = _ASA_ACCESS_GROUP_RE.match(line.strip())
        if not m:
            continue
        acl_name, direction, zone = m.group(1), m.group(2).lower(), m.group(3)
        iface = nameif_to_iface.get(zone)
        if iface is None:
            continue
        if direction == "in":
            iface.acl_in = acl_name
        else:
            iface.acl_out = acl_name


def _extract_asa_nat_objects(parsed: ParsedConfig, raw_text: str) -> None:
    """Collect ``object network <name>`` / ``object-group network <name>``
    names (existence only -- NOT a full object-model resolution, per
    DESIGN.md 4.2.1's confirmed scope) into ``parsed.nat_objects``. These
    feed requirement H's NAT-related WAN signal in a later phase; the actual
    ``nat (inside,outside) ...`` statement text is left to the free-text
    routing summary like everything else this module does not structurally
    model.
    """
    for line in raw_text.splitlines():
        m = _ASA_OBJECT_RE.match(line.strip())
        if m:
            parsed.nat_objects.add(m.group(1))


# ---------------------------------------------------------------------------
# NX-OS vPC domain id (config_converter extension, DESIGN.md 4.9)
# ---------------------------------------------------------------------------

_VPC_DOMAIN_RE = re.compile(r"^vpc\s+domain\s+(\d+)\s*$", re.IGNORECASE)


def _extract_vpc_domain(parsed: ParsedConfig, raw_text: str) -> None:
    """Best-effort top-level ``vpc domain <n>`` extraction (NX-OS only).

    Only the domain id itself is needed -- see DESIGN.md 4.9 and
    ``synthesize_portchannel_member_links()`` in topology_mapper.py, which
    uses it to scope same-batch ``vpc peer-link`` pairing so a batch
    containing more than one independent vPC pair is not cross-wired. A
    device with no such line (any non-NX-OS platform, or an NX-OS device not
    running vPC at all) simply leaves ``parsed.vpc_domain`` at its ``None``
    default, which the pairing logic treats as its own single shared
    "unscoped" bucket -- exactly as safe as today's behaviour whenever there
    are 0 or 1 such devices, and conservatively falls back to Dummy_L2
    synthesis (rather than guessing) if 3+ turn up in that bucket.
    """
    for line in raw_text.splitlines():
        m = _VPC_DOMAIN_RE.match(line.strip())
        if m:
            parsed.vpc_domain = int(m.group(1))
            return  # NX-OS supports at most one non-dual-domain vPC domain per device in this tool's scope


# ---------------------------------------------------------------------------
# Best-effort BGP external-peer extraction (config_converter extension,
# DESIGN.md 4.8 signal 7)
# ---------------------------------------------------------------------------

_BGP_BLOCK_RE = re.compile(r"^router\s+bgp\s+(\d+)", re.IGNORECASE)
_BGP_NEIGHBOR_RE = re.compile(r"^neighbor\s+(\S+)\s+remote-as\s+(\d+)", re.IGNORECASE)


_IOSXR_BGP_NEIGHBOR_BARE_RE = re.compile(r"^neighbor\s+(\S+)\s*$", re.IGNORECASE)
_IOSXR_BGP_REMOTE_AS_RE = re.compile(r"^remote-as\s+(\d+)\s*$", re.IGNORECASE)


def _extract_bgp(parsed: ParsedConfig, raw_text: str) -> None:
    """Best-effort ``router bgp <asn>`` / ``neighbor <ip> remote-as <asn>``
    extraction. NOT a full BGP config model — network statements, route-maps,
    address-families, etc. are intentionally left to the free-text routing
    summary (``_collect_routing_summary``); only the local ASN and each
    neighbour's declared remote ASN are captured here, since that is all
    requirement H's "external BGP peer" WAN signal needs.

    Phase 1c (IOS-XR, DESIGN.md 4.2.1) addition: IOS-XR's commit-model CLI
    puts ``neighbor <ip>`` and its ``remote-as <asn>`` on two SEPARATE,
    nested lines (as opposed to classic IOS's single-line ``neighbor <ip>
    remote-as <asn>``) — a small ``pending_neighbor_ip`` state machine below
    resolves this the same way Phase 1b's HSRP nested-block handling does in
    ``_extract_interfaces_sectionwise()``. Inert for classic IOS/IOS-XE/NX-OS
    input, which never emits a bare ``neighbor <ip>`` line without a
    same-line ``remote-as``.
    """
    lines = raw_text.splitlines()
    n = len(lines)
    i = 0
    while i < n:
        s = lines[i].strip()
        m = _BGP_BLOCK_RE.match(s)
        if m:
            parsed.local_asn = m.group(1)
            i += 1
            pending_neighbor_ip: Optional[str] = None
            while i < n:
                cs = lines[i].strip()
                if cs == "" or cs == "!":
                    i += 1
                    break
                if _SECTION_BOUNDARY_RE.match(cs):
                    break
                m2 = _BGP_NEIGHBOR_RE.match(cs)
                if m2:
                    parsed.bgp_peers.append(BgpPeer(neighbor_ip=m2.group(1), remote_as=m2.group(2)))
                    pending_neighbor_ip = None
                    i += 1
                    continue
                if pending_neighbor_ip is not None:
                    m3 = _IOSXR_BGP_REMOTE_AS_RE.match(cs)
                    if m3:
                        parsed.bgp_peers.append(BgpPeer(neighbor_ip=pending_neighbor_ip, remote_as=m3.group(1)))
                        pending_neighbor_ip = None
                        i += 1
                        continue
                m4 = _IOSXR_BGP_NEIGHBOR_BARE_RE.match(cs)
                if m4:
                    pending_neighbor_ip = m4.group(1)
                    i += 1
                    continue
                pending_neighbor_ip = None
                i += 1
            continue
        i += 1


# ---------------------------------------------------------------------------
# Bandwidth-limit (QoS policy-map) pre-scan (config_converter extension,
# DESIGN.md 4.8 signal 9 / 8.3 decision 8)
# ---------------------------------------------------------------------------

# NX-OS optionally tags a policy-map with its MQC feature type
# ("policy-map type qos|queuing|network-qos <name>", Phase 1b) — IOS/IOS-XE's
# plain "policy-map <name>" is unaffected since the "type ..." group is optional.
_POLICY_MAP_RE = re.compile(r"^policy-map\s+(?:type\s+\S+\s+)?(\S+)", re.IGNORECASE)
_BANDWIDTH_LIMIT_ACTION_RE = re.compile(r"^(?:shape\s+average\b|police\b|priority\b)", re.IGNORECASE)


def _scan_bandwidth_limited_policies(raw_text: str) -> Set[str]:
    """Return the set of ``policy-map`` names whose body contains a
    ``shape average`` / ``police`` / ``priority`` action anywhere under any of
    its classes. Run BEFORE interface extraction so a ``service-policy
    input|output <name>`` line on an interface can be resolved to a
    bandwidth-limit verdict in the same pass (DESIGN.md 4.8 signal 9).

    Deliberately name-only (does not track which class within the policy the
    action lives under, nor the numeric rate) — matching this module's
    "best-effort bandwidth-limit detection" scope (see module docstring).
    """
    limited: Set[str] = set()
    lines = raw_text.splitlines()
    n = len(lines)
    i = 0
    while i < n:
        s = lines[i].strip()
        m = _POLICY_MAP_RE.match(s)
        if m:
            name = m.group(1)
            i += 1
            while i < n:
                cs = lines[i].strip()
                if cs == "" or cs == "!":
                    i += 1
                    break
                if _SECTION_BOUNDARY_RE.match(cs):
                    break
                if _BANDWIDTH_LIMIT_ACTION_RE.match(cs):
                    limited.add(name)
                i += 1
            continue
        i += 1
    return limited


# ---------------------------------------------------------------------------
# Routing summary extraction (BGP / OSPF / EVPN / NVE / HSRP / PIM / etc.)
# — ported, unchanged, from cml_converter/src/config_parser.py.
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

def parse_all(raw_configs: Dict[str, str]) -> Dict[str, ParsedConfig]:
    """Parse a {filename_or_label: raw_text} dict into {hostname_or_label:
    ParsedConfig}.

    DESIGN.md section 4.1.1 decision 12: the incoming ``label`` (typically a
    filename stem, or ``<filename_stem>__<n>`` when ``convert.py``'s
    ``_split_multi_device_blob()`` split one file into multiple devices) is
    reconciled against each ``ParsedConfig``'s own ``hostname`` field —
    ``hostname`` wins whenever it was successfully parsed, since it is the
    more meaningful device identifier for the resulting NS diagram. When two
    different labels resolve to the same hostname (e.g. an operator error, or
    a hostname that collides with an unrelated file's stem), a numeric
    suffix is appended to keep every entry addressable rather than silently
    dropping one.

    Phase 1a scope note: the actual reconciliation *notes* (label-vs-hostname
    mismatches, collisions) are not yet routed into ``config_report.md`` —
    that plumbing is added when ``convert.py``'s report-writing is built out
    in a later sub-phase; for now this function keeps the dict itself
    correct and silently drops nothing.
    """
    out: Dict[str, ParsedConfig] = {}
    for label, raw in raw_configs.items():
        parsed = parse_running_config(raw, hostname_hint=label)
        parsed.source_filename = label
        key = parsed.hostname or label
        if key in out:
            suffix = 2
            candidate = f"{key}_{suffix}"
            while candidate in out:
                suffix += 1
                candidate = f"{key}_{suffix}"
            key = candidate
        out[key] = parsed
    return out
