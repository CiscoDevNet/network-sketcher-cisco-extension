#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""cv_to_ns_commands.py
================================================================================
Cisco Cyber Vision  ->  Network Sketcher command generator (cv_converter)
================================================================================

Converts Cisco Cyber Vision CSV exports into a Network Sketcher CLI command
script that draws an OT network diagram laid out along the **Purdue model /
CPwE / IEC 62443** zone hierarchy (Enterprise -> IDMZ -> Industrial Zone ->
Cell/Area Zone), instead of the office-network layout used by sna_converter.

It consumes one or both of the two Cyber Vision exports:

  * networkNodes-*.csv  -- the asset inventory (nodes / vertices).
        Columns: ID; Device Name; Device Custom Name; Device Type;
        Component Name; Component Custom Name; Group; First/Last Activity;
        IP; MAC; Risk Score; External Communication; Tags; Activities; Vuln;
        Var; VLAN ID; Vendor; OS; Model; Project; Hardware/Firmware Version;
        Serial Number; Sensors; License Required
  * activities-*.csv    -- the communications (edges / conduits).
        Columns: Device1; Component 1 - Name/Custom/Group/Industrial Impact/
        IP/MAC; Device2; Component 2 - ...; Creation Time; Last Activity;
        Tags; Stored flows; Events; Packets; Bytes

Both files are semicolon (';') delimited.

Outputs (written to the output directory):

  * gen_master_commands.txt   -- Network Sketcher CLI script (Phase 1..6)
  * gen_flow_list.csv         -- [Flow_List] paste sheet (src,dst,proto,svc,Mbps)
  * gen_zone_assignment.csv   -- each Group -> Purdue zone + decision basis
  * gen_conduit_report.csv    -- IEC 62443 cross-zone conduit analysis (Phase 3)
  * out_of_scope.csv          -- excluded noise / unjoined assets, with reasons

Design references (Cisco CPwE / IEC 62443, via the documentation MCP):
  CPwE_CIPSec_CVD.pdf, Industrial-AutomationDG.pdf, IA_Networking_Solution_Brief.pdf

Python 3.8+ standard library only.  No third-party dependencies.
"""

from __future__ import annotations

import argparse
import csv
import ipaddress
import json
import math
import os
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

# Console may be a legacy codepage (e.g. cp1252 on Windows); CV paths/names can
# contain non-ASCII (Japanese folder name). Force UTF-8 for our own output.
for _stream in ("stdout", "stderr"):
    try:
        getattr(sys, _stream).reconfigure(encoding="utf-8")
    except Exception:
        pass

# --------------------------------------------------------------------------- #
# Defaults
# --------------------------------------------------------------------------- #

# Default folder to auto-detect the CV CSV exports in (and write outputs to);
# override with --input-dir / --output-dir. Relative to the current directory,
# matching sna_converter's convention.
DEFAULT_IO_DIR = "Input_data"
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "cv_to_ns_config.json")

# Purdue zone bands, ordered top (enterprise) -> bottom (cell/area).
ZONE_ENTERPRISE = "ENTERPRISE"
ZONE_IDMZ = "IDMZ"
ZONE_INDUSTRIAL = "INDUSTRIAL"
ZONE_CELL = "CELL"
ZONE_ORDER = [ZONE_ENTERPRISE, ZONE_IDMZ, ZONE_INDUSTRIAL, ZONE_CELL]

# Numeric "altitude" of a band, used for cross-zone (conduit) jump analysis.
ZONE_LEVEL = {ZONE_ENTERPRISE: 4.5, ZONE_IDMZ: 3.5, ZONE_INDUSTRIAL: 3.0, ZONE_CELL: 2.0}

# Aggregate-area names for the upper (non-cell) bands.
AREA_ENTERPRISE = "Enterprise-Zone"
AREA_IDMZ = "IDMZ"
AREA_INDUSTRIAL = "Industrial-Zone"

# Synthesised CPwE infrastructure devices.
DEV_ENT_CORE = "ENT-Core"
DEV_ENT_EDGE = "ENT-Edge"   # Enterprise access L2 switch, placed left of ENT-Core
DEV_IDMZ_FW = "IDMZ-FW"
DEV_IDMZ_SW = "IDMZ-SW"   # DMZ L2 switch, placed left of IDMZ-FW, holds straddlers
DEV_IND_CORE = "IND-Core"
DEV_IND_EDGE = "IND-Edge"   # Industrial access L2 switch, placed left of IND-Core

# Attribute-sheet cell colours (matches sna_converter's palette / RULE 16).
# The 'Default' column carries a coloured cell:  \"['DEVICE',[R,G,B]]\".
C_INFRA = (200, 200, 200)   # light gray  — devices NOT in the CV export (inferred infra)
C_SERVER = (255, 204, 204)  # light red   — servers / controllers / OT assets
C_PC = (255, 255, 204)      # light yellow — client PCs / workstations
C_NET = (235, 241, 222)     # light green — real network gear (router / firewall)

# --------------------------------------------------------------------------- #
# Classification keyword tables (kept intentionally MINIMAL; the JSON config
# group_zone_override is the authoritative place to pin site-specific names).
# --------------------------------------------------------------------------- #

GROUP_KEYWORDS = [
    # (substring, zone)  -- first match wins; checked case-insensitively.
    ("idmz", ZONE_IDMZ),
    ("dmz", ZONE_IDMZ),
    ("remote access", ZONE_IDMZ),
    ("enterprise", ZONE_ENTERPRISE),
    ("office", ZONE_ENTERPRISE),
    ("corporate", ZONE_ENTERPRISE),
    ("business", ZONE_ENTERPRISE),
    ("engineering station", ZONE_INDUSTRIAL),
    ("management station", ZONE_INDUSTRIAL),
    ("windows station", ZONE_INDUSTRIAL),
    ("control center", ZONE_INDUSTRIAL),
    ("net management", ZONE_INDUSTRIAL),
    ("historian", ZONE_INDUSTRIAL),
    ("scada", ZONE_INDUSTRIAL),
    ("operations", ZONE_INDUSTRIAL),
    ("cyber vision", ZONE_INDUSTRIAL),
    ("edge intelligence", ZONE_INDUSTRIAL),
    ("demo baseline", ZONE_INDUSTRIAL),
    # Cell/Area Zone physical-process hints (checked last; all map to CELL).
    ("cell", ZONE_CELL),
    ("furnace", ZONE_CELL),
    ("machine", ZONE_CELL),
    ("production line", ZONE_CELL),
    ("substation", ZONE_CELL),
    ("process bus", ZONE_CELL),
    ("station bus", ZONE_CELL),
]

# Device Type -> Purdue level (0..4).  Drives within-cell ordering, stencil and
# the per-group band inference when the group name gives no hint.
DEVTYPE_LEVEL = {
    "io module": 0, "camera": 0, "sensor": 0,
    "controller": 1, "slave": 1, "plc": 1, "rtu": 1,
    "master": 2, "opc server": 2, "scada station": 2, "hmi": 2,
    "host config server": 2,
    "engineering station": 3, "net management server": 3, "time server": 3,
    "dns server": 3, "web server": 3, "remote admin server": 3,
    "host config client": 3, "windows": 3, "http client": 3,
    "https client": 3, "routing capability": 3,
    "remote access gateway": 3,
}

# Device Type -> Network Sketcher stencil (limited NS stencil vocabulary).
DEVTYPE_STENCIL = {
    "io module": "Server", "camera": "Server", "sensor": "Server",
    "controller": "Server", "slave": "Server", "plc": "Server", "rtu": "Server",
    "master": "Server", "opc server": "Server", "host config server": "Server",
    "scada station": "PC", "hmi": "PC",
    "engineering station": "PC", "windows": "PC", "host config client": "Server",
    "http client": "PC", "https client": "PC",
    "net management server": "Server", "time server": "Server",
    "dns server": "Server", "web server": "Server", "remote admin server": "Server",
    "remote access gateway": "Firewall", "routing capability": "Router",
}

# Tag -> (proto, port, service-name).  OT protocols first in priority, then IT.
TAG_PROTO = {
    # OT / ICS protocols
    "modbus": ("TCP", "502", "Modbus"),
    "dnp3": ("TCP", "20000", "DNP3"),
    "iec-104": ("TCP", "2404", "IEC-104"),
    "iec104": ("TCP", "2404", "IEC-104"),
    "ethernetip": ("TCP", "44818", "EtherNet/IP"),
    "profinet": ("TCP", "34962", "PROFINET"),
    "s7": ("TCP", "102", "S7comm"),
    "s7plus": ("TCP", "102", "S7comm-Plus"),
    "opc ua": ("TCP", "4840", "OPC-UA"),
    "deltav protocol": ("UDP", "18507", "DeltaV"),
    "vnet": ("UDP", "0", "Yokogawa-Vnet/IP"),
    "bacnet": ("UDP", "47808", "BACnet"),
    # IT protocols
    "https": ("TCP", "443", "HTTPS"),
    "http": ("TCP", "80", "HTTP"),
    "web": ("TCP", "80", "Web"),
    "dns": ("UDP", "53", "DNS"),
    "smb": ("TCP", "445", "SMB"),
    "netbios": ("UDP", "137", "NetBIOS"),
    "netbios name service": ("UDP", "137", "NetBIOS-NS"),
    "ping": ("ICMP", "0", "ICMP"),
}
# Priority order for picking the representative service tag of a flow.
TAG_PRIORITY = [
    "modbus", "dnp3", "iec-104", "iec104", "ethernetip", "profinet", "s7",
    "s7plus", "opc ua", "deltav protocol", "vnet", "bacnet",
    "https", "http", "smb", "dns", "web", "netbios name service", "netbios", "ping",
]

DEFAULT_NOISE_GROUPS = [
    "Broadcast Components", "IPv6 Components", "Packet Reply", "Multicast",
    "To be investigated", "Packet Replay", "",
]


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

def load_config(path: str) -> dict:
    """Read cv_to_ns_config.json; tolerate missing file. Only 'value' is read."""
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[warn] could not read config {path}: {exc}", file=sys.stderr)
        return {}
    out = {}
    for key, blob in raw.items():
        if isinstance(blob, dict) and "value" in blob:
            out[key] = blob["value"]
        else:
            out[key] = blob
    return out


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def sanitize(name: str) -> str:
    """Keep NS-safe characters (no quotes / brackets). Collapse whitespace."""
    if name is None:
        return ""
    keep = []
    for ch in str(name).strip():
        if ch.isalnum() or ch in " -_.+/":
            keep.append(ch)
    s = "".join(keep).strip()
    while "  " in s:
        s = s.replace("  ", " ")
    return s


def norm(s: Optional[str]) -> str:
    return (s or "").strip()


def is_real_ip(ip: str) -> bool:
    """True only for routable unicast IPv4 we want to draw."""
    ip = norm(ip)
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if addr.version != 4:
        return False
    if (addr.is_multicast or addr.is_loopback or addr.is_link_local
            or addr.is_unspecified or addr.is_reserved):
        return False
    if ip == "255.255.255.255":
        return False
    return True


def split_tags(raw: str) -> List[str]:
    return [t.strip() for t in norm(raw).split("/") if t.strip()]


def slug(name: str, maxlen: int = 6) -> str:
    """Short uppercase alnum code for switch names (e.g. 'Drilling Machine'->'DRILL')."""
    s = "".join(ch for ch in str(name) if ch.isalnum())
    return (s[:maxlen] or "AREA").upper()


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #

@dataclass
class Asset:
    key: Tuple[str, str]          # (ip, mac) identity
    name: str                     # raw display candidate
    group: str
    device_type: str = ""
    ip: str = ""
    mac: str = ""
    vendor: str = ""
    os: str = ""
    model: str = ""
    firmware: str = ""
    risk: str = ""
    impact: str = ""              # industrial impact (from activities)
    tags: List[str] = field(default_factory=list)
    activities: int = 0
    vuln: str = ""
    vlan_id: str = ""
    # assigned during processing
    zone: str = ""
    level: int = 1
    area: str = ""
    devname: str = ""             # unique NS device name
    port: str = ""                # endpoint L1 port (e.g. 'GigabitEthernet 0/0')
    switch: str = ""              # switch it connects to


@dataclass
class Edge:
    a_ip: str
    a_mac: str
    a_name: str
    a_group: str
    a_impact: str
    b_ip: str
    b_mac: str
    b_name: str
    b_group: str
    b_impact: str
    tags: List[str]
    bytes: int
    packets: int
    dur_s: float = 0.0   # activity duration (Last Activity - Creation Time), seconds


# --------------------------------------------------------------------------- #
# CSV detection + parsing
# --------------------------------------------------------------------------- #

def read_semicolon_csv(path: str) -> Tuple[List[str], List[List[str]]]:
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh, delimiter=";"))
    if not rows:
        return [], []
    return rows[0], rows[1:]


def detect_files(input_dir: str, nodes_arg: str, acts_arg: str) -> Tuple[Optional[str], Optional[str]]:
    """Resolve the nodes file and the activities file, by CLI arg or header sniff."""
    nodes = nodes_arg if nodes_arg else None
    acts = acts_arg if acts_arg else None
    if nodes and not os.path.isabs(nodes):
        nodes = os.path.join(input_dir, nodes)
    if acts and not os.path.isabs(acts):
        acts = os.path.join(input_dir, acts)
    if nodes and acts:
        return nodes, acts
    if not os.path.isdir(input_dir):
        return nodes, acts
    for fn in sorted(os.listdir(input_dir)):
        if not fn.lower().endswith(".csv"):
            continue
        full = os.path.join(input_dir, fn)
        try:
            with open(full, "r", encoding="utf-8-sig") as fh:
                header = fh.readline()
        except OSError:
            continue
        h = header.lower()
        if not acts and "component 1 - name" in h and "component 2 - name" in h:
            acts = full
        elif not nodes and "device type" in h and "risk score" in h:
            nodes = full
    return nodes, acts


def col_index(header: List[str]) -> Dict[str, int]:
    return {norm(c).lower(): i for i, c in enumerate(header)}


def parse_nodes(path: str) -> Dict[Tuple[str, str], Asset]:
    header, rows = read_semicolon_csv(path)
    idx = col_index(header)

    def g(row, name):
        i = idx.get(name.lower())
        return norm(row[i]) if i is not None and i < len(row) else ""

    assets: Dict[Tuple[str, str], Asset] = {}
    for row in rows:
        if not any(norm(c) for c in row):
            continue
        ip = g(row, "IP")
        mac = g(row, "MAC")
        key = (ip, mac)
        name = (g(row, "Device Custom Name") or g(row, "Component Custom Name")
                or g(row, "Component Name") or g(row, "Device Name") or ip)
        a = assets.get(key)
        if a is None:
            a = Asset(key=key, name=name, group=g(row, "Group"))
            assets[key] = a
        # merge / fill the richest values seen for this (ip, mac)
        a.name = a.name or name
        a.group = a.group or g(row, "Group")
        a.device_type = a.device_type or g(row, "Device Type")
        a.ip = a.ip or ip
        a.mac = a.mac or mac
        a.vendor = a.vendor or g(row, "Vendor")
        a.os = a.os or g(row, "OS")
        a.model = a.model or g(row, "Model")
        a.firmware = a.firmware or g(row, "Firmware Version")
        a.risk = a.risk or g(row, "Risk Score")
        a.vuln = a.vuln or g(row, "Vuln")
        a.vlan_id = a.vlan_id or g(row, "VLAN ID")
        for t in split_tags(g(row, "Tags")):
            if t not in a.tags:
                a.tags.append(t)
        try:
            a.activities = max(a.activities, int(g(row, "Activities") or 0))
        except ValueError:
            pass
    return assets


def parse_cv_time(s: str) -> Optional[float]:
    """Parse a Cyber Vision timestamp ('2022-10-23 13:10:31.755 +0000 UTC') to
    epoch seconds. Returns None if unparseable."""
    s = norm(s)
    if not s:
        return None
    s = s.replace(" UTC", "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f %z", "%Y-%m-%d %H:%M:%S %z"):
        try:
            return datetime.strptime(s, fmt).timestamp()
        except ValueError:
            continue
    return None


def parse_activities(path: str) -> List[Edge]:
    header, rows = read_semicolon_csv(path)
    idx = col_index(header)

    def g(row, name):
        i = idx.get(name.lower())
        return norm(row[i]) if i is not None and i < len(row) else ""

    def to_int(s):
        try:
            return int(float(s))
        except (ValueError, TypeError):
            return 0

    edges: List[Edge] = []
    for row in rows:
        if not any(norm(c) for c in row):
            continue
        # Duration analog to SNA's activeDuration: Last Activity - Creation Time.
        t0 = parse_cv_time(g(row, "Creation Time"))
        t1 = parse_cv_time(g(row, "Last Activity"))
        dur_s = (t1 - t0) if (t0 is not None and t1 is not None and t1 > t0) else 0.0
        edges.append(Edge(
            a_ip=g(row, "Component 1 - IP"), a_mac=g(row, "Component 1 - MAC"),
            a_name=g(row, "Component 1 - Name"), a_group=g(row, "Component 1 - Group"),
            a_impact=g(row, "Component 1 - Group Industrial Impact"),
            b_ip=g(row, "Component 2 - IP"), b_mac=g(row, "Component 2 - MAC"),
            b_name=g(row, "Component 2 - Name"), b_group=g(row, "Component 2 - Group"),
            b_impact=g(row, "Component 2 - Group Industrial Impact"),
            tags=split_tags(g(row, "Tags")),
            bytes=to_int(g(row, "Bytes")), packets=to_int(g(row, "Packets")),
            dur_s=dur_s,
        ))
    return edges


# --------------------------------------------------------------------------- #
# Zone classification
# --------------------------------------------------------------------------- #

def classify_group(group: str, devtypes: Counter, overrides: dict) -> Tuple[str, str]:
    """Return (zone, basis) for a CV group."""
    g = norm(group)
    if g in overrides:
        return overrides[g], "config override"
    low = g.lower()
    for kw, zone in GROUP_KEYWORDS:
        if kw in low:
            return zone, f"keyword '{kw}'"
    # IDMZ only when a Remote Access Gateway is the DOMINANT device type of the
    # group (a lone gateway inside a physical cell should not move the cell).
    if devtypes:
        top_dt, top_n = devtypes.most_common(1)[0]
        if "remote access gateway" in top_dt.lower():
            return ZONE_IDMZ, "dominant device type 'Remote Access Gateway'"
    # Infer from majority device-type level.
    levels = []
    for dt, cnt in devtypes.items():
        lvl = DEVTYPE_LEVEL.get(dt.lower())
        if lvl is not None:
            levels.extend([lvl] * cnt)
    if levels:
        avg = sum(levels) / len(levels)
        if avg >= 3.0:
            return ZONE_INDUSTRIAL, "device-type majority L3"
        return ZONE_CELL, "device-type majority L0-L2"
    return ZONE_CELL, "default (OT cell/area)"


def device_level(asset: Asset) -> int:
    lvl = DEVTYPE_LEVEL.get(asset.device_type.lower())
    if lvl is not None:
        return lvl
    return {ZONE_ENTERPRISE: 4, ZONE_IDMZ: 3, ZONE_INDUSTRIAL: 3, ZONE_CELL: 1}.get(asset.zone, 1)


def device_stencil(asset: Asset) -> str:
    st = DEVTYPE_STENCIL.get(asset.device_type.lower())
    if st:
        return st
    return "PC" if asset.zone == ZONE_ENTERPRISE else "Server"


def _default_cell(rgb: Tuple[int, int, int]) -> str:
    r, g, b = rgb
    # escaped coloured cell for the 'Default' column (RULE 16 / sna_converter)
    return "\\\"['DEVICE',[%d,%d,%d]]\\\"" % (r, g, b)


def stencil_color(stencil: str) -> Tuple[int, int, int]:
    """Role colour for a REAL (imported) asset, by Network Sketcher stencil."""
    if stencil == "PC":
        return C_PC
    if stencil in ("Router", "Firewall"):
        return C_NET
    return C_SERVER  # Server / others


# --------------------------------------------------------------------------- #
# Service / protocol resolution from tags
# --------------------------------------------------------------------------- #

def resolve_service(tags: List[str]) -> Optional[Tuple[str, str, str]]:
    """Pick the representative (proto, port, name) for a flow from its tags."""
    low = [t.lower() for t in tags]
    for key in TAG_PRIORITY:
        if key in low:
            return TAG_PROTO[key]
    return None


# --------------------------------------------------------------------------- #
# Main conversion
# --------------------------------------------------------------------------- #

def convert(nodes_path: Optional[str], acts_path: Optional[str], out_dir: str, cfg: dict) -> dict:
    exclude_noise = bool(cfg.get("exclude_noise_groups", True))
    noise_groups = set(g.strip() for g in cfg.get("noise_groups", DEFAULT_NOISE_GROUPS))
    synth_infra = bool(cfg.get("synthesize_infrastructure", True))
    overrides = dict(cfg.get("group_zone_override", {}))
    vlan_base = int(cfg.get("vlan_base", 101))
    devices_per_row = int(cfg.get("devices_per_row", 6))

    out_of_scope: List[Tuple[str, str, str]] = []  # (name, ip, reason)

    # ---- 1. assets -------------------------------------------------------- #
    assets: Dict[Tuple[str, str], Asset] = {}
    if nodes_path:
        assets = parse_nodes(nodes_path)

    edges: List[Edge] = parse_activities(acts_path) if acts_path else []

    # If no nodes file, synthesise assets from activity endpoints.
    if not assets and edges:
        for e in edges:
            for ip, mac, name, group in ((e.a_ip, e.a_mac, e.a_name, e.a_group),
                                         (e.b_ip, e.b_mac, e.b_name, e.b_group)):
                key = (ip, mac)
                if key not in assets:
                    assets[key] = Asset(key=key, name=name or ip, group=group, ip=ip, mac=mac)

    # Backfill industrial impact onto assets from the activity endpoints.
    impact_by_key: Dict[Tuple[str, str], str] = {}
    for e in edges:
        if e.a_impact:
            impact_by_key.setdefault((e.a_ip, e.a_mac), e.a_impact)
        if e.b_impact:
            impact_by_key.setdefault((e.b_ip, e.b_mac), e.b_impact)
    for key, a in assets.items():
        a.impact = impact_by_key.get(key, a.impact)

    # ---- 2. noise / scope filtering -------------------------------------- #
    live: Dict[Tuple[str, str], Asset] = {}
    for key, a in assets.items():
        grp = norm(a.group)
        if exclude_noise and grp in noise_groups:
            out_of_scope.append((a.name, a.ip, f"noise group '{grp or '(empty)'}'"))
            continue
        if not is_real_ip(a.ip):
            out_of_scope.append((a.name, a.ip, "no routable IPv4"))
            continue
        live[key] = a

    if not live:
        raise SystemExit("No in-scope assets found. Check input files / noise filters.")

    # ---- 3. group -> zone classification --------------------------------- #
    group_devtypes: Dict[str, Counter] = defaultdict(Counter)
    for a in live.values():
        if a.device_type:
            group_devtypes[a.group][a.device_type] += 1

    group_zone: Dict[str, str] = {}
    group_basis: Dict[str, str] = {}
    for grp in sorted({a.group for a in live.values()}):
        zone, basis = classify_group(grp, group_devtypes.get(grp, Counter()), overrides)
        group_zone[grp] = zone
        group_basis[grp] = basis

    for a in live.values():
        a.zone = group_zone[a.group]
        a.level = device_level(a)

    # ---- 3b. IDMZ straddlers --------------------------------------------- #
    # Assets in the Activity list that communicate with BOTH the Enterprise zone
    # and the Industrial zone are IT/OT bridges. Per IEC 62443 / CPwE they belong
    # in the IDMZ, so relocate them there; later they hang under a dedicated DMZ
    # L2 switch placed to the left of the IDMZ firewall.
    ent_side = set(cfg.get("idmz_enterprise_zones", [ZONE_ENTERPRISE]))
    ind_side = set(cfg.get("idmz_industrial_zones", [ZONE_INDUSTRIAL]))
    detect_straddlers = bool(cfg.get("idmz_straddler_detection", True))
    straddlers: List[str] = []
    if detect_straddlers and edges:
        a_by_key = {a.key: a for a in live.values()}
        a_by_ip: Dict[str, Asset] = {}
        a_by_name: Dict[str, Asset] = {}
        for a in live.values():
            a_by_ip.setdefault(a.ip, a)
            a_by_name.setdefault(norm(a.name), a)

        def _resolve(ip, mac, name):
            return (a_by_key.get((ip, mac)) or a_by_ip.get(ip)
                    or a_by_name.get(norm(name)))

        peer_zones: Dict[Tuple[str, str], set] = defaultdict(set)
        for e in edges:
            sa = _resolve(e.a_ip, e.a_mac, e.a_name)
            sb = _resolve(e.b_ip, e.b_mac, e.b_name)
            if sa and sb and sa is not sb:
                peer_zones[sa.key].add(sb.zone)
                peer_zones[sb.key].add(sa.zone)
        for a in live.values():
            if a.zone == ZONE_IDMZ:
                continue
            pz = peer_zones.get(a.key, set())
            if (pz & ent_side) and (pz & ind_side):
                a.zone = ZONE_IDMZ
                a.idmz_straddler = True  # type: ignore[attr-defined]
                straddlers.append(a.devname or a.name or a.ip)

    # ---- 4. unique device names ------------------------------------------ #
    used = set()
    for a in sorted(live.values(), key=lambda x: (x.zone, x.group, x.ip)):
        base = sanitize(a.name) or sanitize(a.ip) or "node"
        cand = base
        n = 2
        while cand in used:
            cand = f"{base} {n}"
            n += 1
        used.add(cand)
        a.devname = cand

    # ---- 5. assign areas -------------------------------------------------- #
    # Cell/Area groups each become their own area; upper bands are aggregated.
    cell_groups = sorted({a.group for a in live.values() if a.zone == ZONE_CELL})
    cell_area_name: Dict[str, str] = {}
    used_area = {AREA_ENTERPRISE, AREA_IDMZ, AREA_INDUSTRIAL}
    for grp in cell_groups:
        nm = sanitize(grp) or "Cell"
        cand = nm
        n = 2
        while cand in used_area:
            cand = f"{nm} {n}"
            n += 1
        used_area.add(cand)
        cell_area_name[grp] = cand

    for a in live.values():
        if a.zone == ZONE_ENTERPRISE:
            a.area = AREA_ENTERPRISE
        elif a.zone == ZONE_IDMZ:
            a.area = AREA_IDMZ
        elif a.zone == ZONE_INDUSTRIAL:
            a.area = AREA_INDUSTRIAL
        else:
            a.area = cell_area_name[a.group]

    has_ent = any(a.zone == ZONE_ENTERPRISE for a in live.values())
    has_idmz = any(a.zone == ZONE_IDMZ for a in live.values())
    has_ind = any(a.zone == ZONE_INDUSTRIAL for a in live.values())
    has_cell = bool(cell_groups)

    # ---- 6. synthesise CPwE infrastructure & ports ----------------------- #
    # Build the device->switch wiring and allocate ports.
    port_dn: Dict[str, int] = defaultdict(int)   # next access (downlink) port
    port_up: Dict[str, int] = defaultdict(int)   # next uplink/interswitch port
    l1_links: List[Tuple[str, str, str, str]] = []

    def access_port(switch: str) -> str:
        port_dn[switch] += 1
        return f"GigabitEthernet 1/0/{port_dn[switch]}"

    def uplink_port(switch: str) -> str:
        port_up[switch] += 1
        return f"GigabitEthernet 0/{port_up[switch]}"

    infra_devices: List[str] = []
    # Switch backbone: ENT-Core - IDMZ-FW - IND-Core (and cell switches under IND-Core).
    need_ind_core = synth_infra and (has_ind or has_cell)
    need_ent_core = synth_infra and has_ent
    need_idmz_fw = synth_infra and need_ent_core and (need_ind_core)

    if need_ent_core:
        infra_devices.append(DEV_ENT_CORE)
    if need_idmz_fw or (synth_infra and has_idmz):
        infra_devices.append(DEV_IDMZ_FW)
    if need_ind_core:
        infra_devices.append(DEV_IND_CORE)

    if need_idmz_fw:
        p1, p2 = uplink_port(DEV_ENT_CORE), uplink_port(DEV_IDMZ_FW)
        l1_links.append((DEV_ENT_CORE, DEV_IDMZ_FW, p1, p2))
        p3, p4 = uplink_port(DEV_IDMZ_FW), uplink_port(DEV_IND_CORE)
        l1_links.append((DEV_IDMZ_FW, DEV_IND_CORE, p3, p4))
    elif need_ent_core and need_ind_core:
        p1, p2 = uplink_port(DEV_ENT_CORE), uplink_port(DEV_IND_CORE)
        l1_links.append((DEV_ENT_CORE, DEV_IND_CORE, p1, p2))

    # DMZ L2 switch (left of the IDMZ firewall): straddlers hang below it, and it
    # uplinks to IDMZ-FW which hosts the DMZ gateway SVI.
    has_idmz_now = any(a.zone == ZONE_IDMZ for a in live.values())
    need_idmz_sw = synth_infra and has_idmz_now and DEV_IDMZ_FW in infra_devices
    idmz_trunk_ports: Optional[Tuple[str, str]] = None
    if need_idmz_sw:
        infra_devices.append(DEV_IDMZ_SW)
        up = uplink_port(DEV_IDMZ_SW)
        dn = uplink_port(DEV_IDMZ_FW)
        l1_links.append((DEV_IDMZ_SW, DEV_IDMZ_FW, up, dn))
        idmz_trunk_ports = (up, dn)

    # Enterprise / Industrial access L2 switches: host endpoints hang off these
    # (left of their core), which uplink to the core (mirrors the DMZ switch).
    ent_trunk_ports: Optional[Tuple[str, str]] = None
    ind_trunk_ports: Optional[Tuple[str, str]] = None
    need_ind_edge = synth_infra and need_ind_core and has_ind
    need_ent_edge = synth_infra and need_ent_core and has_ent
    if need_ind_edge:
        infra_devices.append(DEV_IND_EDGE)
        up = uplink_port(DEV_IND_EDGE)
        dn = uplink_port(DEV_IND_CORE)
        l1_links.append((DEV_IND_EDGE, DEV_IND_CORE, up, dn))
        ind_trunk_ports = (up, dn)
    if need_ent_edge:
        infra_devices.append(DEV_ENT_EDGE)
        up = uplink_port(DEV_ENT_EDGE)
        dn = uplink_port(DEV_ENT_CORE)
        l1_links.append((DEV_ENT_EDGE, DEV_ENT_CORE, up, dn))
        ent_trunk_ports = (up, dn)

    # Per cell-area access switch, uplinked to IND-Core.
    cell_switch: Dict[str, str] = {}
    cell_trunk_ports: Dict[str, Tuple[str, str]] = {}  # group -> (cellsw_up, indcore_dn)
    if synth_infra:
        for grp in cell_groups:
            sw = f"CELLSW-{slug(grp)}"
            cand, n = sw, 2
            while cand in cell_switch.values():
                cand = f"{sw}{n}"
                n += 1
            cell_switch[grp] = cand
            infra_devices.append(cand)
            if need_ind_core:
                up = uplink_port(cand)
                dn = uplink_port(DEV_IND_CORE)
                l1_links.append((cand, DEV_IND_CORE, up, dn))
                cell_trunk_ports[grp] = (up, dn)

    # Wire each endpoint to its switch.
    def switch_for(a: Asset) -> Optional[str]:
        if not synth_infra:
            return None
        if a.zone == ZONE_ENTERPRISE and need_ent_core:
            return DEV_ENT_EDGE if DEV_ENT_EDGE in infra_devices else DEV_ENT_CORE
        if a.zone == ZONE_IDMZ:
            if DEV_IDMZ_SW in infra_devices:
                return DEV_IDMZ_SW
            if DEV_IDMZ_FW in infra_devices:
                return DEV_IDMZ_FW
        if a.zone == ZONE_INDUSTRIAL and need_ind_core:
            return DEV_IND_EDGE if DEV_IND_EDGE in infra_devices else DEV_IND_CORE
        if a.zone == ZONE_CELL:
            return cell_switch.get(a.group)
        return DEV_IND_CORE if need_ind_core else None

    for a in sorted(live.values(), key=lambda x: (x.area, x.level, x.devname)):
        sw = switch_for(a)
        a.switch = sw or ""
        a.port = "GigabitEthernet 0/0"
        if sw:
            sp = access_port(sw)
            l1_links.append((a.devname, sw, a.port, sp))
            a.sw_port = sp  # type: ignore[attr-defined]

    # ---- 7. VLAN / subnet assignment per area ---------------------------- #
    # Group endpoints by area; one VLAN+/24 per area, SVI on nearest L3 device.
    area_assets: Dict[str, List[Asset]] = defaultdict(list)
    for a in live.values():
        area_assets[a.area].append(a)

    area_vlan: Dict[str, int] = {}
    area_gateway: Dict[str, str] = {}
    area_l3dev: Dict[str, str] = {}
    vlan_n = vlan_base
    # deterministic area order: enterprise, idmz, industrial, then cells
    ordered_areas = []
    if has_ent:
        ordered_areas.append(AREA_ENTERPRISE)
    if has_idmz:
        ordered_areas.append(AREA_IDMZ)
    if has_ind:
        ordered_areas.append(AREA_INDUSTRIAL)
    ordered_areas.extend(cell_area_name[g] for g in cell_groups)

    area_of_cell_group = {v: k for k, v in cell_area_name.items()}

    for area in ordered_areas:
        members = [a for a in area_assets.get(area, []) if is_real_ip(a.ip)]
        if not members:
            continue
        # dominant /24
        nets = Counter(a.ip.rsplit(".", 1)[0] for a in members)
        net24 = nets.most_common(1)[0][0]
        # gateway host: .1, or .254 if .1 already used by a device
        used_hosts = {a.ip for a in members}
        gw = f"{net24}.1"
        if gw in used_hosts:
            gw = f"{net24}.254"
        # L3 device hosting the SVI
        if area == AREA_ENTERPRISE and need_ent_core:
            l3 = DEV_ENT_CORE
        elif area == AREA_IDMZ and DEV_IDMZ_FW in infra_devices:
            l3 = DEV_IDMZ_FW
        elif need_ind_core:
            l3 = DEV_IND_CORE
        else:
            continue
        area_vlan[area] = vlan_n
        area_gateway[area] = gw
        area_l3dev[area] = l3
        vlan_n += 1

    # ---- 8. emit Network Sketcher commands ------------------------------- #
    lines: List[str] = []

    def add(cmd: str):
        lines.append(cmd)

    # Phase 1: area_location (top -> bottom rows)
    rows_layout: List[List[str]] = []
    if has_ent:
        rows_layout.append([AREA_ENTERPRISE])
    if DEV_IDMZ_FW in infra_devices or has_idmz:
        rows_layout.append([AREA_IDMZ])
    if has_ind or need_ind_core:
        rows_layout.append([AREA_INDUSTRIAL])
    if has_cell:
        rows_layout.append([cell_area_name[g] for g in cell_groups])
    layout = "[" + ",".join("[" + ",".join(f"'{a}'" for a in row) + "]" for row in rows_layout) + "]"
    add(f'add area_location "{layout}"')

    # Phase 1: device_location per area
    def grid_for(area: str, lead: List[str],
                 lead_rows: Optional[List[List[str]]] = None,
                 tail_rows: Optional[List[List[str]]] = None) -> str:
        rows: List[List[str]] = ([list(r) for r in lead_rows]
                                 if lead_rows is not None else [[d] for d in lead])
        eps = [a.devname for a in sorted(area_assets.get(area, []),
                                         key=lambda x: (-x.level, x.devname))]
        for i in range(0, len(eps), devices_per_row):
            rows.append(eps[i:i + devices_per_row])
        if tail_rows:
            rows.extend([list(r) for r in tail_rows])
        body = "[" + ",".join("[" + ",".join(f"'{d}'" for d in r) + "]" for r in rows) + "]"
        return f"add device_location \"['{area}',{body}]\""

    def edge_row_for(area: str, edge: Optional[str], core: Optional[str],
                     at_bottom: bool = False) -> str:
        # The access switch + core sit ALONE on their own row (no endpoints share
        # it). Network Sketcher aligns equal-width rows column-for-column, so we
        # pad every row to a fixed width with empty-string cells: the endpoint
        # grid stays left-aligned (devices_per_row wide), the switch sits directly
        # above/below the grid's rightmost column, and the core is placed one
        # column further right -- over an empty column, so NOTHING is under (or
        # over) the core. The switch row is the TOP row for Industrial (core
        # faces up) or the BOTTOM row for Enterprise (core at the area bottom).
        eps = [a.devname for a in sorted(area_assets.get(area, []),
                                         key=lambda x: (-x.level, x.devname))]
        dpr = devices_per_row
        width = dpr + 2  # 2 extra columns: a gap then the core, right of the grid
        tiers = [eps[i:i + dpr] for i in range(0, len(eps), dpr)]
        tiers = [row + [""] * (width - len(row)) for row in tiers]  # pad to width
        switch_rows: List[List[str]] = []
        if edge or core:
            sw = [""] * width
            if edge:
                sw[dpr - 1] = edge   # above/below the grid's rightmost column
            if core:
                sw[dpr + 1] = core   # one gap to the right; empty column below/above
            switch_rows = [sw]
        rows: List[List[str]] = (tiers + switch_rows) if at_bottom else (switch_rows + tiers)
        if not rows:
            rows = [[]]
        body = "[" + ",".join("[" + ",".join(f"'{d}'" for d in r) + "]" for r in rows) + "]"
        return f"add device_location \"['{area}',{body}]\""

    if has_ent:
        # ENT-Edge (left) + ENT-Core (right) on one row; Enterprise hosts lined up
        # to the left of ENT-Edge, none beneath ENT-Core.
        ent_edge = DEV_ENT_EDGE if DEV_ENT_EDGE in infra_devices else None
        ent_core = DEV_ENT_CORE if need_ent_core else None
        add(edge_row_for(AREA_ENTERPRISE, ent_edge, ent_core, at_bottom=True))
    if DEV_IDMZ_FW in infra_devices or has_idmz:
        if DEV_IDMZ_SW in infra_devices and DEV_IDMZ_FW in infra_devices:
            # DMZ L2 switch sits to the LEFT of the firewall (same row).
            idmz_lead = [[DEV_IDMZ_SW, DEV_IDMZ_FW]]
        elif DEV_IDMZ_FW in infra_devices:
            idmz_lead = [[DEV_IDMZ_FW]]
        else:
            idmz_lead = []
        add(grid_for(AREA_IDMZ, [], lead_rows=idmz_lead))
    if has_ind or need_ind_core:
        # IND-Edge (left) + IND-Core (right) on one row; Industrial hosts lined up
        # to the left of IND-Edge, none beneath IND-Core.
        ind_edge = DEV_IND_EDGE if DEV_IND_EDGE in infra_devices else None
        ind_core = DEV_IND_CORE if need_ind_core else None
        add(edge_row_for(AREA_INDUSTRIAL, ind_edge, ind_core, at_bottom=True))
    for grp in cell_groups:
        area = cell_area_name[grp]
        add(grid_for(area, [cell_switch[grp]] if grp in cell_switch else []))

    # Phase 2: l1_link_bulk
    if l1_links:
        seen = set()
        parts = []
        for a_dev, b_dev, a_p, b_p in l1_links:
            key = frozenset({(a_dev, a_p), (b_dev, b_p)})
            if key in seen:
                continue
            seen.add(key)
            parts.append(f"['{a_dev}','{b_dev}','{a_p}','{b_p}']")
        add('add l1_link_bulk "[' + ",".join(parts) + ']"')

    # Phase 2.5: port_info_bulk (all devices 1Gbps copper)
    all_devs = sorted({a.devname for a in live.values()} | set(infra_devices))
    if all_devs:
        dev_field = "[" + ",".join(f"'{d}'" for d in all_devs) + "]"
        add(f"rename port_info_bulk \"[[{dev_field},'_ALL_',['1Gbps','Full','1000BASE-T']]]\"")

    # Phase 3: virtual_port (SVIs) + l2_segment + Phase 4: ip_address
    # Duplicate IPs are ALLOWED (OT environments routinely reuse the same address
    # across isolated cells). We only avoid assigning the SAME gateway IP twice on
    # the SAME L3 device (which NS rejects); endpoint IPs are emitted as-is,
    # duplicates included.
    assigned_ips: set = set()          # (device, ip) already placed on an L3 SVI
    skipped_ip = 0
    for area in ordered_areas:
        if area not in area_vlan:
            continue
        vlan = area_vlan[area]
        l3 = area_l3dev[area]
        gw = area_gateway[area]
        add(f"add virtual_port_bulk \"[['{l3}',['Vlan {vlan}']]]\"")
        add(f"add l2_segment_bulk \"[['{l3}','Vlan {vlan}',['Vlan{vlan}']]]\"")
        # trunk binding for cell areas (CELLSW uplink + IND-Core downlink)
        grp = area_of_cell_group.get(area)
        if grp and grp in cell_trunk_ports:
            up, dn = cell_trunk_ports[grp]
            sw = cell_switch[grp]
            add(f"add l2_segment_bulk \"[['{sw}','{up}',['Vlan{vlan}']],['{l3}','{dn}',['Vlan{vlan}']]]\"")
        # trunk binding for the IDMZ (IDMZ-SW uplink + IDMZ-FW downlink)
        if area == AREA_IDMZ and idmz_trunk_ports:
            up, dn = idmz_trunk_ports
            add(f"add l2_segment_bulk \"[['{DEV_IDMZ_SW}','{up}',['Vlan{vlan}']],['{DEV_IDMZ_FW}','{dn}',['Vlan{vlan}']]]\"")
        # trunk binding for the Enterprise / Industrial access switches
        if area == AREA_ENTERPRISE and ent_trunk_ports:
            up, dn = ent_trunk_ports
            add(f"add l2_segment_bulk \"[['{DEV_ENT_EDGE}','{up}',['Vlan{vlan}']],['{DEV_ENT_CORE}','{dn}',['Vlan{vlan}']]]\"")
        if area == AREA_INDUSTRIAL and ind_trunk_ports:
            up, dn = ind_trunk_ports
            add(f"add l2_segment_bulk \"[['{DEV_IND_EDGE}','{up}',['Vlan{vlan}']],['{DEV_IND_CORE}','{dn}',['Vlan{vlan}']]]\"")
        # access-port bindings on the endpoint switches
        binds = []
        for a in area_assets.get(area, []):
            sp = getattr(a, "sw_port", "")
            if a.switch and sp:
                binds.append(f"['{a.switch}','{sp}',['Vlan{vlan}']]")
        if binds:
            add('add l2_segment_bulk "[' + ",".join(binds) + ']"')
        # SVI gateway IP -- skip only if this exact gateway IP is already on this
        # same L3 device (NS rejects a duplicate IP on the same device).
        if (l3, gw) not in assigned_ips:
            add(f"add ip_address_bulk \"[['{l3}','Vlan {vlan}',['{gw}/24']]]\"")
            assigned_ips.add((l3, gw))

    # Phase 4: endpoint IPs (directly on their L1 port, RULE 11.5).
    # Duplicate IPs across devices are allowed (OT reuses addresses per cell).
    ip_rows = []
    for a in sorted(live.values(), key=lambda x: x.devname):
        if not is_real_ip(a.ip):
            continue
        ip_rows.append(f"['{a.devname}','{a.port}',['{a.ip}/24']]")
    if ip_rows:
        add('add ip_address_bulk "[' + ",".join(ip_rows) + ']"')

    # Phase 6: attribute_bulk (Device Name, Default[coloured], Model, OS, Stencil Type)
    # Real (imported) assets are coloured by role; synthesised/inferred devices
    # (not present in the CV export) are uniformly light gray.
    attr_rows = ["['Device Name', 'Default', 'Model', 'OS', 'Stencil Type']"]
    for a in sorted(live.values(), key=lambda x: x.devname):
        model = sanitize(a.model) or sanitize(a.vendor) or sanitize(a.device_type) or "OT Asset"
        os_ = sanitize(a.os) or "-"
        st = device_stencil(a)
        cell = _default_cell(stencil_color(st))
        attr_rows.append(f"['{a.devname}', {cell}, '{model}', '{os_}', '{st}']")
    # synthesised infra attributes (CPwE-appropriate models) -- ALWAYS gray
    infra_attr = {
        DEV_ENT_CORE: ("Catalyst 9500", "IOS-XE", "L3Switch"),
        DEV_ENT_EDGE: ("Catalyst 9300", "IOS-XE", "Switch"),
        DEV_IDMZ_FW: ("Secure Firewall 3100", "FTD", "Firewall"),
        DEV_IDMZ_SW: ("Catalyst 9300", "IOS-XE", "Switch"),
        DEV_IND_CORE: ("Catalyst IE9300", "IOS-XE", "L3Switch"),
        DEV_IND_EDGE: ("Catalyst IE3400", "IOS-XE", "Switch"),
    }
    gray = _default_cell(C_INFRA)
    for dev in infra_devices:
        if dev in infra_attr:
            m, o, s = infra_attr[dev]
        else:  # cell switch
            m, o, s = ("Catalyst IE3400", "IOS-XE", "Switch")
        attr_rows.append(f"['{dev}', {gray}, '{m}', '{o}', '{s}']")
    add('rename attribute_bulk "[' + ",".join(attr_rows) + ']"')

    # ---- 9. flow list ----------------------------------------------------- #
    key_to_asset = {a.key: a for a in live.values()}
    name_to_asset: Dict[str, Asset] = {}
    ip_to_asset: Dict[str, Asset] = {}
    for a in live.values():
        name_to_asset.setdefault(norm(a.name), a)
        ip_to_asset.setdefault(a.ip, a)

    def resolve_endpoint(ip: str, mac: str, name: str) -> Optional[Asset]:
        a = key_to_asset.get((ip, mac))
        if a:
            return a
        a = ip_to_asset.get(ip)
        if a:
            return a
        return name_to_asset.get(norm(name))

    # Max bandwidth uses the SAME formula as sna_converter:
    #   Mbps = transferBytes * 8 / activeDuration(seconds) / 1e6, keeping the MAX
    # per (src, dst, proto, service) flow. CV has no per-session activeDuration, so
    # the duration analog is (Last Activity - Creation Time); flows whose duration
    # is <= 0 are skipped (rate cannot be computed), exactly like SNA.
    flow_bw: Dict[Tuple[str, str, str, str], float] = {}  # (src,dst,proto,svc)->max Mbps
    conduit_agg: Dict[Tuple[str, str], dict] = {}        # (srcZone,dstZone)->stats
    flow_dropped = 0
    for e in edges:
        sa = resolve_endpoint(e.a_ip, e.a_mac, e.a_name)
        sb = resolve_endpoint(e.b_ip, e.b_mac, e.b_name)
        if not sa or not sb or sa.devname == sb.devname:
            flow_dropped += 1
            continue
        svc = resolve_service(e.tags)
        # conduit (cross-zone) accounting -- independent of L3 protocol presence
        if sa.zone != sb.zone:
            za, zb = sorted((sa.zone, sb.zone), key=lambda z: -ZONE_LEVEL.get(z, 0))
            c = conduit_agg.setdefault((za, zb), {"count": 0, "bytes": 0, "protos": set()})
            c["count"] += 1
            c["bytes"] += e.bytes
            for t in e.tags:
                c["protos"].add(t)
        if not svc:
            continue
        proto, port, name = svc
        if proto not in ("TCP", "UDP", "ICMP"):
            continue
        if not (e.dur_s and e.dur_s > 0):   # need a positive duration for a rate
            continue
        mbps = e.bytes * 8.0 / e.dur_s / 1e6
        label = f"{name}({port})" if port and port != "0" else name
        k = (sa.devname, sb.devname, proto, label)
        if mbps > flow_bw.get(k, 0.0):
            flow_bw[k] = mbps

    # ---- write outputs ---------------------------------------------------- #
    os.makedirs(out_dir, exist_ok=True)

    cmd_path = os.path.join(out_dir, "gen_master_commands.txt")
    with open(cmd_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")

    def fmt_bw(x: float) -> str:
        # Same plain-decimal formatting as sna_converter (keeps <1 values, no
        # scientific notation).
        if x <= 0:
            return "0"
        if x >= 1:
            return ("%.2f" % x).rstrip("0").rstrip(".")
        d = min(max(2 - int(math.floor(math.log10(x))), 2), 12)
        return ("%.*f" % (d, x)).rstrip("0").rstrip(".") or "0"

    flow_path = os.path.join(out_dir, "gen_flow_list.csv")
    with open(flow_path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        fh.write("[Flow_List]\n")
        w.writerow(["No", "Source Device Name", "Destination Device Name",
                    "TCP/UDP/ICMP", "Service name(Port)", "Max. bandwidth(Mbps)",
                    "Manually routing path settings", "Automatic routing path settings"])
        i = 1
        for (src, dst, proto, label), mbps in sorted(
                flow_bw.items(), key=lambda kv: (-kv[1], kv[0][0], kv[0][1], kv[0][3])):
            w.writerow([i, src, dst, proto, label, fmt_bw(mbps), "", ""])
            i += 1

    zone_path = os.path.join(out_dir, "gen_zone_assignment.csv")
    with open(zone_path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["CV Group", "Assigned Zone (Purdue)", "Decision basis",
                    "Asset count", "Device types", "Area"])
        counts = Counter(a.group for a in live.values())
        for grp in sorted(group_zone):
            dts = ", ".join(f"{k}x{v}" for k, v in group_devtypes.get(grp, Counter()).most_common())
            area = (AREA_ENTERPRISE if group_zone[grp] == ZONE_ENTERPRISE else
                    AREA_IDMZ if group_zone[grp] == ZONE_IDMZ else
                    AREA_INDUSTRIAL if group_zone[grp] == ZONE_INDUSTRIAL else
                    cell_area_name.get(grp, ""))
            w.writerow([grp, group_zone[grp], group_basis[grp], counts.get(grp, 0), dts, area])

    conduit_path = os.path.join(out_dir, "gen_conduit_report.csv")
    with open(conduit_path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Higher Zone", "Lower Zone", "Purdue jump", "Activities",
                    "Total Bytes", "Protocols", "IEC 62443 assessment"])
        for (za, zb), c in sorted(conduit_agg.items(), key=lambda kv: -kv[1]["bytes"]):
            jump = round(abs(ZONE_LEVEL.get(za, 0) - ZONE_LEVEL.get(zb, 0)), 1)
            pair = {za, zb}
            if ZONE_ENTERPRISE in pair and (ZONE_CELL in pair or ZONE_INDUSTRIAL in pair):
                assess = "REVIEW: Enterprise<->OT conduit bypasses IDMZ (IEC 62443 violation)"
            elif jump >= 1.5:
                assess = "REVIEW: spans multiple Purdue levels"
            else:
                assess = "OK: adjacent zones"
            protos = "/".join(sorted(p for p in c["protos"] if p)[:8])
            w.writerow([za, zb, jump, c["count"], c["bytes"], protos, assess])

    oos_path = os.path.join(out_dir, "out_of_scope.csv")
    with open(oos_path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Name", "IP", "Reason excluded"])
        for name, ip, reason in out_of_scope:
            w.writerow([name, ip, reason])

    return {
        "assets_total": len(assets),
        "assets_in_scope": len(live),
        "excluded": len(out_of_scope),
        "edges": len(edges),
        "flows": len(flow_bw),
        "flows_dropped": flow_dropped,
        "conduits": len(conduit_agg),
        "infra_devices": len(infra_devices),
        "idmz_straddlers": straddlers,
        "commands": len(lines),
        "zones": {z: sum(1 for a in live.values() if a.zone == z) for z in ZONE_ORDER},
        "out_dir": out_dir,
        "cmd_path": cmd_path,
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv=None):
    p = argparse.ArgumentParser(description="Cisco Cyber Vision -> Network Sketcher converter")
    p.add_argument("--input-dir", default=DEFAULT_IO_DIR,
                   help="Folder to auto-detect the CV CSV exports in.")
    p.add_argument("--output-dir", default=None,
                   help="Folder for generated outputs (default: same as --input-dir).")
    p.add_argument("--nodes", default=None, help="networkNodes CSV (overrides auto-detect).")
    p.add_argument("--activities", default=None, help="activities CSV (overrides auto-detect).")
    p.add_argument("--config", default=CONFIG_PATH, help="Path to cv_to_ns_config.json.")
    args = p.parse_args(argv)

    out_dir = args.output_dir or args.input_dir
    cfg = load_config(args.config)
    nodes, acts = detect_files(args.input_dir, args.nodes, args.activities)

    print("Cisco Cyber Vision -> Network Sketcher (cv_converter)")
    print(f"  nodes file      : {nodes or '(none)'}")
    print(f"  activities file : {acts or '(none)'}")
    print(f"  output dir      : {out_dir}")
    if not nodes and not acts:
        raise SystemExit("No input CSVs found. Use --nodes / --activities or --input-dir.")

    stats = convert(nodes, acts, out_dir, cfg)

    print("\n--- summary ---")
    print(f"  assets: {stats['assets_in_scope']} in scope "
          f"({stats['excluded']} excluded) of {stats['assets_total']} total")
    print(f"  zones : " + ", ".join(f"{z}={n}" for z, n in stats["zones"].items()))
    print(f"  edges : {stats['edges']} activities -> {stats['flows']} flows "
          f"({stats['flows_dropped']} dropped), {stats['conduits']} cross-zone conduits")
    print(f"  infra : {stats['infra_devices']} synthesised devices")
    sd = stats.get("idmz_straddlers", [])
    print(f"  IDMZ  : {len(sd)} straddler(s) -> DMZ switch"
          + (f" [{', '.join(sd[:8])}{'…' if len(sd) > 8 else ''}]" if sd else ""))
    print(f"  wrote : {stats['commands']} commands -> {stats['cmd_path']}")
    print("  outputs: gen_master_commands.txt, gen_flow_list.csv, "
          "gen_zone_assignment.csv, gen_conduit_report.csv, out_of_scope.csv")


if __name__ == "__main__":
    main()
