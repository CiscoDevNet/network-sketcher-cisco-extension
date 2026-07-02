# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""Shared helpers over NDFC switch / link / VRF / Network objects.

Used by the underlay (switch naming + role) and overlay (VRF / Network template
parsing, attachment resolution) mappers. All helpers degrade gracefully to
empty / default results when a field is missing, so a partial export still
works.

NDFC stores the bulk of a VRF's / Network's L2-L3 attributes inside a
JSON-*encoded string* (``vrfTemplateConfig`` / ``networkTemplateConfig``);
:func:`template_config` decodes it whether it arrives as a string or an already
parsed dict.
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Switches
# ---------------------------------------------------------------------------

def switch_display_name(sw: Dict[str, Any], naming: str = "name") -> str:
    """Device display name per the ``switch_naming`` policy.

    ``name``      -> logicalName / sysName / hostName (falls back to IP)
    ``name_ip``   -> '<name>-<ip>'
    ``ip``        -> the management IP only
    ``serial``    -> the serial number
    """
    nm = (sw.get("logicalName") or sw.get("sysName") or sw.get("hostName")
          or "").strip()
    ip = (sw.get("ipAddress") or sw.get("mgmtAddress") or "").strip()
    serial = (sw.get("serialNumber") or "").strip()
    if naming == "ip" and ip:
        return ip
    if naming == "serial" and serial:
        return serial
    if naming == "name_ip" and nm and ip:
        return f"{nm}-{ip}"
    return nm or ip or serial or "switch"


def switch_role(sw: Dict[str, Any]) -> str:
    """Best-available NDFC role string for a switch."""
    return (sw.get("switchRoleEnum") or sw.get("switchRole") or sw.get("role")
            or "").strip()


def switch_version(sw: Dict[str, Any]) -> str:
    """NX-OS version string ('release' is the friendly form, e.g. '10.6(1)')."""
    return (sw.get("release") or sw.get("version") or "").strip()


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------

# NDFC link-types that represent a real intra-fabric cable (vs an external/
# neighbour adjacency). 'ethisl' = fabric ISL (leaf<->spine, vPC peer-link);
# 'lan_neighbor_link' = a discovered adjacency, often to a switch OUTSIDE the
# fabric inventory (ISN / edge / core).
FABRIC_LINK_TYPES = {"ethisl", "isl", "fabric_link", "intra_fabric"}


def link_endpoints(link: Dict[str, Any]) -> Optional[tuple]:
    """Return ((sysA, ifA, roleA, modelA), (sysB, ifB, roleB, modelB)) or None."""
    a = link.get("sw1-info") or {}
    b = link.get("sw2-info") or {}
    sa, sb = a.get("sw-sys-name"), b.get("sw-sys-name")
    if not sa or not sb:
        return None
    return (
        (sa, a.get("if-name") or "", a.get("switch-role") or "", a.get("sw-model-name") or ""),
        (sb, b.get("if-name") or "", b.get("switch-role") or "", b.get("sw-model-name") or ""),
    )


def is_fabric_link(link: Dict[str, Any]) -> bool:
    return (link.get("link-type") or "").lower() in FABRIC_LINK_TYPES


# ---------------------------------------------------------------------------
# VRF / Network template configs
# ---------------------------------------------------------------------------

def template_config(obj: Dict[str, Any], *keys: str) -> Dict[str, Any]:
    """Decode an NDFC template-config blob.

    The config may be under any of ``keys`` (e.g. 'networkTemplateConfig',
    'vrfTemplateConfig', 'displayValues') and may be a JSON-encoded *string* or
    an already-parsed dict. Returns {} when absent / unparseable.
    """
    for k in keys:
        val = obj.get(k)
        if isinstance(val, dict):
            return val
        if isinstance(val, str) and val.strip():
            try:
                parsed = json.loads(val)
                if isinstance(parsed, dict):
                    return parsed
            except (ValueError, TypeError):
                continue
    return {}


def network_l2_info(net: Dict[str, Any]) -> Dict[str, Any]:
    """Extract the fields the overlay needs from a Network object.

    Returns: {name, vni, vrf, vlan_id, gw_v4, gw_v6, vlan_name, description}.
    """
    cfg = template_config(net, "networkTemplateConfig", "displayValues")
    vlan = cfg.get("vlanId") or net.get("vlanId") or ""
    try:
        vlan_id = int(str(vlan)) if str(vlan).strip() else None
    except (ValueError, TypeError):
        vlan_id = None
    gw4 = (cfg.get("gatewayIpAddress") or "").strip()
    gw6 = (cfg.get("gatewayIpV6Address") or "").strip()
    return {
        "name": net.get("networkName") or cfg.get("networkName") or "",
        "display": net.get("displayName") or net.get("networkName") or "",
        "vni": net.get("networkId") or cfg.get("networkId") or "",
        "vrf": net.get("vrf") or cfg.get("vrfName") or "",
        "vlan_id": vlan_id,
        "gw_v4": gw4,
        "gw_v6": gw6,
        "vlan_name": cfg.get("vlanName") or "",
        "description": cfg.get("intfDescription") or "",
    }


def vrf_l3_info(vrf: Dict[str, Any]) -> Dict[str, Any]:
    """Extract the fields the overlay needs from a VRF object.

    Returns: {name, vni, vlan_id, vlan_name}.
    """
    cfg = template_config(vrf, "vrfTemplateConfig", "displayValues")
    vlan = cfg.get("vrfVlanId") or vrf.get("vrfVlanId") or ""
    try:
        vlan_id = int(str(vlan)) if str(vlan).strip() else None
    except (ValueError, TypeError):
        vlan_id = None
    return {
        "name": vrf.get("vrfName") or cfg.get("vrfName") or "",
        "vni": vrf.get("vrfId") or cfg.get("vrfSegmentId") or "",
        "vlan_id": vlan_id,
        "vlan_name": cfg.get("vrfVlanName") or "",
    }


# ---------------------------------------------------------------------------
# Optional richer sources: vPC pairs, Endpoint Locator hosts, interface detail
# (all tolerant — NDFC field names vary by release; absent data yields empty).
# ---------------------------------------------------------------------------

def _first(obj: Dict[str, Any], *keys: str) -> str:
    for k in keys:
        v = obj.get(k)
        if v not in (None, "", 0, "0"):
            return str(v)
    return ""


def vpc_pairs(vpc_list: List[Dict[str, Any]]) -> Dict[str, Dict[str, str]]:
    """{switch_serial: {'peer': <peer_serial>, 'peerName': <name>, 'domain': <id>}}.

    Tolerant of the ``/lan-fabric/rest/vpcpair`` shapes across NDFC releases
    (peerOneSerialNumber / peerOneId / peerOneSwitchName, etc.)."""
    out: Dict[str, Dict[str, str]] = {}
    for p in vpc_list or []:
        s1 = _first(p, "peerOneSerialNumber", "peerOneId", "serialNumberOne", "peerOneDbId")
        s2 = _first(p, "peerTwoSerialNumber", "peerTwoId", "serialNumberTwo", "peerTwoDbId")
        n1 = _first(p, "peerOneSwitchName", "peerOneName", "switchNameOne")
        n2 = _first(p, "peerTwoSwitchName", "peerTwoName", "switchNameTwo")
        dom = _first(p, "domainId", "vpcDomainId", "domain", "useVirtualPeerlink")
        if s1 and s2:
            out[s1] = {"peer": s2, "peerName": n2, "domain": dom}
            out[s2] = {"peer": s1, "peerName": n1, "domain": dom}
    return out


@dataclass
class NdEndpoint:
    ip: str = ""
    mac: str = ""
    vrf: str = ""
    network: str = ""
    switch: str = ""
    port: str = ""
    vlan: str = ""


def _norm_endpoint(e: Dict[str, Any]) -> NdEndpoint:
    return NdEndpoint(
        ip=_first(e, "ip", "ipAddress", "hostIp", "endpointIp", "ipAddr"),
        mac=_first(e, "mac", "macAddress", "hostMac", "endpointMac"),
        vrf=_first(e, "vrf", "vrfName"),
        network=_first(e, "networkName", "network", "l2vniName", "segmentName"),
        switch=_first(e, "switchName", "switch", "nodeName", "attachedSwitch", "leafName"),
        port=_first(e, "port", "interface", "attachIf", "switchPort", "ifName"),
        vlan=_first(e, "vlan", "vlanId", "accessVlan", "dot1qVlan"),
    )


def endpoints_by_network(endpoints: List[Dict[str, Any]],
                         networks: List[Dict[str, Any]]) -> Dict[str, List[NdEndpoint]]:
    """{networkName: [NdEndpoint, ...]} from Endpoint Locator data.

    Endpoints are grouped by their ``networkName`` when present; otherwise the
    endpoint's VLAN is mapped back to a Network via the networks' vlanId so EPL
    exports that only carry a VLAN still attach to the right segment."""
    vlan_to_net: Dict[str, str] = {}
    for net in networks or []:
        info = network_l2_info(net)
        if info["vlan_id"] is not None and info["name"]:
            vlan_to_net[str(info["vlan_id"])] = info["name"]
    out: Dict[str, List[NdEndpoint]] = defaultdict(list)
    for e in endpoints or []:
        ep = _norm_endpoint(e)
        net = ep.network or vlan_to_net.get(ep.vlan, "")
        if net:
            out[net].append(ep)
    return out


# NDFC interface ifType -> a representative NS port spec (speed, duplex, media).
# The virtual N9300v reports speed=None, so type-based defaults are used; a real
# platform's numeric ``speed`` (Mbps) overrides via :func:`_speed_to_ns`.
def _speed_to_ns(speed: Any) -> Optional[str]:
    try:
        mbps = int(speed)
    except (TypeError, ValueError):
        return None
    if mbps <= 0:
        return None
    return {1000: "1Gbps", 10000: "10Gbps", 25000: "25Gbps", 40000: "40Gbps",
            100000: "100Gbps", 400000: "400Gbps"}.get(mbps, f"{mbps // 1000}Gbps")


def interface_portinfo(interfaces: List[Dict[str, Any]]) -> Dict[str, tuple]:
    """{ifName: (speed, duplex, media)} for physical ports of one switch.

    Used to give the underlay real per-port speed when interface detail is
    fetched; falls back to the role-based default otherwise."""
    out: Dict[str, tuple] = {}
    for itf in interfaces or []:
        if (itf.get("ifType") or "") != "INTERFACE_ETHERNET":
            continue
        name = itf.get("ifName")
        if not name:
            continue
        speed = _speed_to_ns(itf.get("speed"))
        duplex = (itf.get("duplex") or "Full").capitalize() if itf.get("duplex") else "Full"
        out[name] = (speed or "10Gbps", duplex, "10GBASE-SR")
    return out


def attachment_switches(attachments: List[Dict[str, Any]], obj_name: str,
                        name_key: str) -> List[str]:
    """Switch sysNames a VRF / Network is attached to.

    NDFC's attachment response groups by object name with a
    ``lanAttachList`` of per-switch entries. ``name_key`` is 'vrfName' or
    'networkName'.
    """
    out: List[str] = []
    for att in attachments or []:
        if att.get(name_key) and att.get(name_key) != obj_name:
            continue
        for entry in att.get("lanAttachList") or att.get("switchDetailsList") or []:
            sw = (entry.get("switchName") or entry.get("switchSerialNo")
                  or entry.get("serialNumber") or "")
            is_att = entry.get("lanAttachState") or entry.get("isLanAttached")
            if sw and (is_att in (None, "DEPLOYED", "PENDING", True, "true")):
                out.append(sw)
    return out
