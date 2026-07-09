# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""Map a device parsed from a raw running-config to an NS Stencil Type +
Model + OS string.

**Phase 1e status (DESIGN.md section 6 roadmap)**: this module is a WORKING,
MCP-verified implementation of ``map_device()`` (real devices) and
``map_inferred_peer()`` (synthesized ``Dummy_<TC>_<n>`` placeholders / the
shared WAN cloud / inferred L2 switches). See ``config_converter/DESIGN.md``
section 4.4 (requirement D) and section 1.2/1.3 (cml_converter /
netbox_converter findings) for the full design. This module has NO live
API/export field to read from (no CML ``node_definition``, no NetBox
``device_role``) — every classification signal comes from the config text
itself:

  * ``ParsedConfig.os_family`` (nxos/ios/iosxe/iosxr/asa/unknown — ALL FIVE
    internal families are Phase 1 scope per DESIGN.md section 4.2.1, decision
    7; the internal ``"asa"`` key is a UNIFIED discriminator for ASA, FTD, and
    FDM-managed FTD alike -- there is no separate ``"ftd"`` internal value,
    since ``config_parser.detect_os_family()`` cannot reliably (and, per
    Cisco documentation, does not need to) tell them apart from the
    ASA-syntax text alone. This unified family is displayed to the user as
    **"ASA(FTD/FDM)"** via ``OS_FAMILY_DISPLAY`` below — from
    ``config_parser.detect_os_family``, ported from cml_converter's function
    of the same name, see DESIGN.md section 4.2).
  * hostname keyword heuristics, ported/merged from TWO existing sources:
      - cml_converter's ``LABEL_KEYWORD_RULES`` (spine/leaf/bgw/border/agg/
        dist/core/access/wan/edge/mpls/fw/asa/ftd/wlc/ap-/server/vm, with a
        (keyword, NS_*, model_hint, os_hint, confidence) shape),
      - 3rd_party/netbox_converter's ``netbox_layout.py`` role-keyword
        dictionaries used for tier assignment (wan_keywords,
        edge_router_base_keywords, core_keywords, endpoint_keywords) — these
        can double as a SECOND, independent classification signal.
  * structural features computed from the parsed interfaces themselves (no
    precedent in any existing converter — this is config_converter-specific):
    interface count / degree, presence of SVIs (-> L3Switch candidate),
    presence of a routing-protocol block (-> Router/L3Switch candidate),
    trunk-heavy vs. access-heavy port mix (-> Switch candidate), WAN-scored
    interfaces present (-> Router/Firewall candidate) — see DESIGN.md
    section 4.8 for the WAN scoring model these features feed into.

Confidence convention used across every converter in this repo (keep it):
- 1.00 : exact match from a real platform field (N/A here — config_converter
         has no such field; the practical ceiling for a *real* device is 0.85)
- 0.85 : inferred from a name/keyword heuristic
- 0.60 : synthesised logical construct (a placeholder peer/WAN-cloud device
         config_converter invents per DESIGN.md section 4.5/4.8 — NOT a
         literal device found in the input corpus)
- 0.40 : pure default — flag for human review in the report
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional


# Allowed NS Stencil Type values (RULE 16). Do not add to this list — these
# are the only stencil types the Network Sketcher engine accepts.
NS_ROUTER = "Router"
NS_L3SWITCH = "L3Switch"
NS_SWITCH = "Switch"
NS_FIREWALL = "Firewall"
NS_WLC = "WLC"
NS_AP = "AP"
NS_SERVER = "Server"
NS_CLOUD = "Cloud"
NS_PHONE = "Phone"
NS_PC = "PC"


@dataclass
class StencilMapping:
    label: str
    node_definition: str        # the source role / construct token (e.g. os_family, or 'inferred_peer')
    image_definition: str       # serial / extra context (audit only) — usually empty for config_converter
    stencil_type: str
    model: str
    os: str
    confidence: float
    reason: str
    tags: List[str]


# Phase 1e (DESIGN.md section 6): merges cml_converter's LABEL_KEYWORD_RULES
# with 3rd_party/netbox_converter's netbox_layout.py role-keyword vocabulary
# into a single table. Each item: (keyword_substring, NS_STENCIL, model_hint,
# os_hint, confidence). Every confidence value is <= 0.85 (config_converter
# never has an exact platform-field match the way CML's node_definition or
# NetBox's device_role do).
#
# ORDER MATTERS: map_device() below walks this list top-to-bottom and returns
# on the FIRST substring hit, so more specific/definitive keywords (explicit
# device-type words like "router"/"rtr"/"asa", or type-disambiguating
# compounds like "bgw") are placed BEFORE generic tier-only keywords
# ("core"/"gw") that would otherwise misfire on a hostname carrying both
# (e.g. bundled sample "CORE-RTR01" must resolve via "rtr" to Router, not via
# "core" to L3Switch -- see config_converter/Input_data/sample1/README.md).
HOSTNAME_KEYWORD_RULES: List[tuple] = [
    # ---- Firewalls / WLC (most specific, OS-name-like keywords first) -----
    ("firewall", NS_FIREWALL, "Firewall", "", 0.80),
    # ASA/FTD/FDM unification (config_converter DESIGN.md 4.2.1): a hostname
    # containing "asa" OR "ftd" is the SAME unified platform family and MUST
    # produce the SAME Model/OS display strings -- previously "ftd" showed a
    # conflicting "Cisco FTD"/"FTD" pair while "asa" showed "Cisco ASA"/"ASA",
    # which no longer makes sense now that both are treated as one identity.
    # Both keyword entries are kept (rather than collapsing to one) purely as
    # independent hostname-based hints (e.g. a device named "FTD-01" has no
    # "asa" substring), but they now agree on the same displayed label.
    ("asa", NS_FIREWALL, "Cisco ASA(FTD/FDM)", "ASA(FTD/FDM)", 0.85),
    ("ftd", NS_FIREWALL, "Cisco ASA(FTD/FDM)", "ASA(FTD/FDM)", 0.85),
    ("fw", NS_FIREWALL, "Firewall", "", 0.55),
    ("wlc", NS_WLC, "Wireless LAN Controller", "", 0.85),
    # ---- L3 tier words that are also unambiguous device-type words --------
    ("bgw", NS_L3SWITCH, "Border Gateway Switch", "", 0.80),
    ("spine", NS_L3SWITCH, "Spine Switch", "", 0.80),
    ("leaf", NS_L3SWITCH, "Leaf Switch", "", 0.80),
    ("border", NS_L3SWITCH, "Border Switch / Router", "", 0.65),
    ("aggregation", NS_L3SWITCH, "Aggregation Switch", "", 0.70),
    ("agg", NS_L3SWITCH, "Aggregation Switch", "", 0.60),
    ("distribution", NS_L3SWITCH, "Distribution Switch", "", 0.70),
    ("dist", NS_L3SWITCH, "Distribution Switch", "", 0.70),
    # ---- Explicit Router words (checked BEFORE the generic "core"/"gw" -----
    # tier words below, so e.g. "CORE-RTR01" resolves to Router via "rtr") --
    ("router", NS_ROUTER, "Router", "", 0.75),
    ("rtr", NS_ROUTER, "Router", "", 0.70),
    ("gateway", NS_ROUTER, "Gateway Router", "", 0.55),
    ("gw", NS_ROUTER, "Gateway Router", "", 0.50),
    ("core", NS_L3SWITCH, "Core Switch", "", 0.65),
    ("edge", NS_ROUTER, "Edge Router", "", 0.60),
    ("isn", NS_ROUTER, "Inter-Site Network Router", "", 0.60),
    ("mpls", NS_ROUTER, "MPLS PE Router", "", 0.55),
    ("wan", NS_ROUTER, "WAN Edge Router", "", 0.55),
    # ---- Access layer -------------------------------------------------------
    ("access", NS_SWITCH, "Access Switch", "", 0.75),
    ("acc", NS_SWITCH, "Access Switch", "", 0.50),
    ("ap-", NS_AP, "Wireless Access Point", "", 0.60),
    # ---- Endpoints ----------------------------------------------------------
    ("printer", NS_SERVER, "Network Printer", "", 0.55),
    ("server", NS_SERVER, "Server", "", 0.70),
    ("srv", NS_SERVER, "Server", "", 0.50),
    ("phone", NS_PHONE, "IP Phone", "", 0.55),
]

# Phase 1e (DESIGN.md section 4.2): os_family (from
# config_parser.detect_os_family) -> a default (model, os) display string.
# cml_converter never wired detect_os_family() into its stencil decision
# (DESIGN.md section 5.2 of the cml_converter investigation) --
# config_converter SHOULD, since it has no other source of platform identity.
#
# ASA/FTD/FDM unification (DESIGN.md section 4.2.1): "asa" is displayed as
# "ASA(FTD/FDM)" -- Cisco documentation confirms ASA, FMC-managed FTD, and
# locally-managed (FDM) FTD all expose the exact same LINA/ASA-syntax
# "show running-config" text this parser targets, so they are shown as one
# unified platform identity rather than three separate labels. There is
# intentionally NO separate "ftd" key here -- detect_os_family() never
# returns that value (see its own docstring for why).
OS_FAMILY_DISPLAY = {
    "nxos": ("Nexus (NX-OS device)", "NX-OS"),
    "ios": ("IOS device", "IOS"),
    "iosxe": ("IOS-XE device", "IOS-XE"),
    "iosxr": ("IOS-XR device", "IOS-XR"),
    "asa": ("ASA(FTD/FDM) device", "ASA(FTD/FDM)"),
    "unknown": ("Unknown platform", ""),
}

# 2-letter inferred-device type codes used by the fixed naming grammar
# `Dummy_<TC>_<n>` (DESIGN.md section 4.5.1, confirmed per section 8.4
# decision 11). Map every NS_* stencil constant an inferred device could use
# to its 2-letter code here so synthesize_inferred_peers()
# (topology_mapper.py) and map_inferred_peer() (below) share one source of
# truth. Interface names on inferred devices are ALWAYS `Dummy <n>`
# (independent 0-based counter), regardless of type code.
DUMMY_TYPE_CODES = {
    NS_ROUTER: "RT",
    NS_L3SWITCH: "L3",
    NS_SWITCH: "L2",
    NS_FIREWALL: "FW",
    NS_WLC: "WL",
    NS_AP: "AP",
    NS_SERVER: "SV",
    NS_CLOUD: "CL",
    NS_PHONE: "PH",
    NS_PC: "PC",
}


def normalise_hostname(name: str) -> str:
    """Canonicalise a hostname for keyword matching ('CORE-SW01' -> 'core-sw01')."""
    return (name or "").strip().lower()


# Structural fallback threshold (DESIGN.md 4.4's "次数" note): a device with
# no hostname-keyword hit but at least this many parsed interfaces and no
# SVI/routing-protocol signal is more likely to be an access switch (lots of
# access ports) than a router (routers typically have few interfaces).
_ACCESS_HEAVY_INTERFACE_THRESHOLD = 8


def map_device(
    name: str,
    os_family: str = "unknown",
    interface_count: int = 0,
    has_svi: bool = False,
    has_routing_protocol: bool = False,
    inferred: bool = False,
) -> StencilMapping:
    """Map one real (config-file-backed) device to a stencil (DESIGN.md 4.4).

    Decision order (each step returns immediately on a hit):
      1. ``os_family == 'asa'`` (the unified ASA/FTD/FDM family, DESIGN.md
         4.2.1 -- there is no separate ``'ftd'`` value to check, since
         ``detect_os_family()`` never produces one) -- this platform family
         IS a firewall by definition, so this OS-level signal outranks any
         hostname keyword.
      2. ``HOSTNAME_KEYWORD_RULES`` (first substring hit wins).
      3. Structural fallback using the parsed interface set: has_svi ->
         L3Switch; has_routing_protocol (no SVI) -> Router; interface_count
         >= ``_ACCESS_HEAVY_INTERFACE_THRESHOLD`` (no SVI/routing) -> Switch.
      4. Default: Router, confidence 0.30 (flagged for human review).

    Confidence convention preserved: a *real* device never exceeds 0.85 here
    (config_converter has no exact-match platform field the way CML's
    node_definition or NetBox's device_role do). ``inferred`` is accepted for
    call-signature symmetry with the rest of this module but is unused here
    -- SYNTHESISED placeholder devices must go through map_inferred_peer()
    below instead, which enforces the separate <=0.60 confidence ceiling.
    """
    del inferred  # see docstring: synthesised devices use map_inferred_peer()
    model_display, os_display = OS_FAMILY_DISPLAY.get(os_family, OS_FAMILY_DISPLAY["unknown"])

    # NOTE: only "asa" is checked here (not a "ftd" alternative) -- "asa" is
    # the single, unified internal discriminator for ASA/FTD/FDM alike
    # (DESIGN.md 4.2.1); detect_os_family() is structurally incapable of
    # producing a "ftd" value, so a defensive `os_family in ("asa", "ftd")`
    # check would only ever be testing a dead branch. See config_parser.py's
    # detect_os_family() docstring for the full rationale.
    if os_family == "asa":
        return StencilMapping(
            label=name, node_definition=os_family, image_definition="",
            stencil_type=NS_FIREWALL, model=model_display, os=os_display,
            confidence=0.85,
            reason=f"os_family='{os_family}' (ASA/FTD/FDM) is always a Firewall platform",
            tags=[],
        )

    hn = normalise_hostname(name)
    for kw, stencil, model_hint, os_hint, confidence in HOSTNAME_KEYWORD_RULES:
        if kw in hn:
            return StencilMapping(
                label=name, node_definition=os_family, image_definition="",
                stencil_type=stencil,
                # NOTE (Phase 1e MCP live-engine verification): do NOT wrap
                # `kw` in quote characters here -- ns_command_builder.py's
                # shared, do-not-customise `_attr_cell()` backslash-escapes
                # embedded apostrophes (`\'`) the way every other converter's
                # Model/OS/reason strings expect, but the live Network
                # Sketcher engine's own `rename attribute_bulk` cell parser
                # does NOT correctly round-trip that escape for the Model
                # column -- it silently drops the backslash before handing
                # the cell to its literal-eval-style parser, which then fails
                # on the resulting unescaped, unbalanced quote ("invalid
                # syntax. Perhaps you forgot a comma?"). Avoiding embedded
                # quote characters entirely sidesteps the engine bug without
                # touching the shared ns_command_builder.py.
                model=f"{model_hint} (inferred from keyword: {kw})",
                os=os_hint or os_display,
                confidence=min(confidence, 0.85),
                reason=f"hostname keyword match '{kw}'",
                tags=[],
            )

    if has_svi:
        return StencilMapping(
            label=name, node_definition=os_family, image_definition="",
            stencil_type=NS_L3SWITCH,
            model=model_display or "L3 device (SVI present)", os=os_display,
            confidence=0.55,
            reason="structural: at least one SVI (Vlan interface) present",
            tags=[],
        )
    if has_routing_protocol:
        return StencilMapping(
            label=name, node_definition=os_family, image_definition="",
            stencil_type=NS_ROUTER,
            model=model_display or "Router (routing protocol configured)",
            os=os_display, confidence=0.55,
            reason="structural: routing protocol (BGP/OSPF/EIGRP/IS-IS) configured, no SVI",
            tags=[],
        )
    if interface_count >= _ACCESS_HEAVY_INTERFACE_THRESHOLD:
        return StencilMapping(
            label=name, node_definition=os_family, image_definition="",
            stencil_type=NS_SWITCH,
            model=model_display or f"Switch ({interface_count} interfaces)",
            os=os_display, confidence=0.45,
            reason=(
                f"structural: {interface_count} interfaces, no SVI/routing "
                "signal -- likely an access switch"
            ),
            tags=[],
        )

    return StencilMapping(
        label=name, node_definition=os_family, image_definition="",
        stencil_type=NS_ROUTER,
        model=model_display or "Unknown device (default: Router)",
        os=os_display, confidence=0.30,
        reason="no hostname keyword or structural signal matched -- REVIEW",
        tags=[],
    )


def map_inferred_peer(
    name: str,
    kind: str,
    reason: str,
    model_hint: str = "",
) -> StencilMapping:
    """Map a SYNTHESISED placeholder device — a peer that config_converter
    invents because no matching device was found in the input corpus
    (requirement E, DESIGN.md section 4.5/4.5.1) or the shared WAN/Internet
    cloud waypoint (requirement H, DESIGN.md section 4.8) or a shared
    connectivity-guarantee waypoint (requirement F, DESIGN.md section 4.6 --
    NOTE: F and H now intentionally share the SAME ``Dummy_CL_1`` device when
    no site hint distinguishes them, per DESIGN.md 4.8's "判定結果の反映").

    The returned mapping's ``label`` MUST follow the fixed naming grammar
    confirmed in DESIGN.md 4.5.1 (decision 11): ``Dummy_<TC>_<n>`` where
    ``<TC>`` comes from ``DUMMY_TYPE_CODES`` above (per-type-code counter,
    NOT a global counter across all inferred devices), with interface names
    ``Dummy <n>`` (independent 0-based counter per device). The deprecated
    ``Peer_<device>_<ifname>`` naming no longer exists.

    TODO: ``kind`` should distinguish at least:
      - "peer"       : a generic unresolved neighbour (NS_ROUTER/Dummy_RT_<n>
                        default, DESIGN.md 4.5.1's Stencil-inference priority
                        list)
      - "l2_switch"   : NS_SWITCH/Dummy_L2_<n>, the inferred hub synthesized
                        by needs_l2_switch_inference()'s 'synthetic_switch'
                        path or the forced k==2 case (DESIGN.md 4.3.3/4.3.6)
      - "wan_cloud"   : NS_CLOUD/Dummy_CL_1, gray (this repo's WayPoint
                        convention for a purely invented waypoint — see
                        template_converter/GUIDE.md "Observed vs. inferred
                        WayPoints"). This is the single shared device for
                        BOTH requirement F's connectivity guarantee and
                        requirement H's WAN classification.
      - "isolated_marker" (DESIGN.md 4.7.1, requirement G) : NOT a stencil
                        per se — closed-environment devices keep their real
                        stencil and are placed in the dedicated isolated
                        area (``cfg['isolated_area_name']``); only their
                        attribute/report note changes here. This function is
                        for genuinely synthesised DEVICES only.

    Every mapping returned here MUST keep confidence <= 0.60 (this repo's
    "synthesised logical construct" ceiling) and its caller MUST set
    ``NSDevice.default_color = (200, 200, 200)`` (gray) — config_converter
    never has a real record behind these, so they never qualify for the
    observed-WayPoint light-blue colour (see GUIDE.md's documented past bug).

    Phase 1e implementation note: only ``kind in {"peer", "l2_switch",
    "wan_cloud"}`` are implemented (the naming/counter logic itself lives in
    ``topology_mapper.py``'s ``synthesize_inferred_peers()`` /
    ``_materialize_l2_switch()``, which pass an already-decided ``name``
    here). The context-based Server/PC branches of DESIGN.md 4.5's priority
    list (items 2/3 — description keywords like "server"/"host"/"printer")
    are deferred to a later phase; every non-WAN, non-L2-switch inferred
    peer defaults to ``NS_ROUTER`` (item 4, confidence 0.30) for now.
    ``"isolated_marker"`` is intentionally NOT handled here, per the
    docstring above (requirement G never synthesises a new device).
    """
    if kind == "wan_cloud":
        stencil, default_model, confidence = (
            NS_CLOUD, "WAN/Internet Cloud (inferred waypoint)", 0.60,
        )
    elif kind == "l2_switch":
        stencil, default_model, confidence = (
            NS_SWITCH, "Inferred L2 Switch (shared-segment hub)", 0.60,
        )
    elif kind == "peer":
        stencil, default_model, confidence = (
            NS_ROUTER, "Inferred unresolved peer device", 0.30,
        )
    else:
        raise ValueError(
            f"map_inferred_peer: unsupported kind={kind!r} "
            "(expected 'peer' | 'l2_switch' | 'wan_cloud')"
        )

    return StencilMapping(
        label=name, node_definition=f"inferred:{kind}", image_definition="",
        stencil_type=stencil, model=model_hint or default_model, os="",
        confidence=confidence, reason=reason, tags=["inferred"],
    )


def to_csv_rows(mappings: List[StencilMapping]) -> List[List[str]]:
    """Render mappings as config_inventory.csv rows (device -> stencil audit)."""
    rows = [["name", "os_family/kind", "context", "stencil_type",
             "model", "os", "confidence", "reason", "tags"]]
    for m in mappings:
        rows.append([
            m.label, m.node_definition, m.image_definition, m.stencil_type,
            m.model, m.os, f"{m.confidence:.2f}", m.reason, ",".join(m.tags)
        ])
    return rows
