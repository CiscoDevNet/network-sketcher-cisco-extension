# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""Shared vManage-model helpers used by both the physical (underlay) and
logical (overlay) mappers -- device naming, area naming, VPN0 interface
filtering, transport-colour resolution, and Service VPN (VRF) interface
selection.

Kept in one module (mirroring ``catc_converter``'s ``catc_topology.py``) so
the two mappers apply IDENTICAL rules for "what counts as a VPN0 transport
interface", "what counts as a Service VPN interface", "which NS area does
this device's site-id map to" and "how a colour is resolved for an IP",
instead of drifting apart.
"""
from __future__ import annotations

import ipaddress
import re
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple

from .sdwan_stencil_mapper import normalise_personality

# Physical interface-type prefixes accepted as a real routed uplink -- a VPN0
# transport uplink for the Underlay, or a Service VPN (VRF) LAN/WAN port for
# the Overlay. Pseudo/system interfaces (NVI0, Sdwan-system-intf,
# vmanage_system, Tunnel*) are deliberately excluded here; Loopback is matched
# separately by ``is_loopback_ifname`` because the Overlay models it as an
# unconnected virtual port (see below).
_PHYSICAL_PREFIXES = (
    "gigabitethernet", "tengigabitethernet", "twentyfivegige", "fortygige",
    "hundredgige", "fastethernet", "ethernet",
)

# vManage VPN ids that are never a customer Service VPN: 0 is the Transport
# VPN (the Underlay's domain), 512 is out-of-band management, and
# 65528-65530 are internal/system VPNs. ALWAYS excluded from the Overlay,
# regardless of the ``excluded_service_vpns`` configuration value.
RESERVED_VPN_IDS = frozenset({0, 512, 65528, 65529, 65530})


def device_personality(dev: Dict[str, Any]) -> str:
    return normalise_personality(dev.get("personality") or "", dev.get("device-model") or "")


def device_is_inferred_personality(dev: Dict[str, Any]) -> bool:
    """True when ``personality`` was absent/unrecognised and the role had to
    be guessed from ``device-model`` (lower stencil confidence -- see
    ``sdwan_stencil_mapper.map_device``)."""
    return not (dev.get("personality") or "").strip()


def device_display_name(dev: Dict[str, Any], naming: str = "hostname") -> str:
    """Resolve a vManage device to a display name.

    ``naming``: ``hostname`` (default, ``host-name``) or ``hostname_ip``
    (``host-name (system-ip)``) for exports with duplicate/blank hostnames.
    """
    host = (dev.get("host-name") or dev.get("system-ip") or dev.get("uuid") or "device").strip()
    if naming == "hostname_ip" and dev.get("system-ip"):
        return f"{host} ({dev['system-ip']})"
    return host or "device"


# Area names are used verbatim as NS area tokens, so anything outside this
# conservative character class is folded to '_'.
_AREA_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_.-]")


def _area_token(raw: Any) -> str:
    """NS-safe area token for a raw ``site-name``, or '' when unusable."""
    token = _AREA_UNSAFE_RE.sub("_", str(raw or "").strip())
    return token if any(ch.isalnum() for ch in token) else ""


def build_site_area_map(devices: List[Dict[str, Any]]) -> Dict[str, str]:
    """Map each ``site-id`` (as a STRING key) to the NS area name to draw it in.

    A real ``/dataservice/device`` record carries a human-readable
    ``site-name`` alongside its numeric ``site-id`` (``site-id 3`` ->
    ``"br1"``), which makes a far more readable area than ``site3``. This
    resolves one name per site-id for BOTH mappers, so the underlay and the
    overlay always agree:

      * the most frequently reported ``site-name`` across that site-id's
        devices wins (alphabetically first on a tie, for determinism);
      * a site-id whose devices report no usable ``site-name`` at all falls
        back to ``site<id>`` (the pre-``site-name`` behaviour);
      * if two DIFFERENT site-ids resolve to the same name, every one of them
        is disambiguated to ``<name>_site<id>`` -- otherwise two genuinely
        separate sites would be fused into a single NS area.

    Devices with no ``site-id`` are absent from the returned map; the caller
    places those in its own fallback area (``default``).
    """
    names_by_site: Dict[str, Counter] = {}
    for dev in devices or []:
        site_id = dev.get("site-id")
        if site_id in (None, ""):
            continue
        key = str(site_id).strip()
        if not key:
            continue
        counter = names_by_site.setdefault(key, Counter())
        token = _area_token(dev.get("site-name"))
        if token:
            counter[token] += 1

    area_by_site: Dict[str, str] = {}
    for key, counter in names_by_site.items():
        if counter:
            area_by_site[key] = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        else:
            area_by_site[key] = f"site{key}"

    sites_by_area: Dict[str, List[str]] = defaultdict(list)
    for key, area in area_by_site.items():
        sites_by_area[area].append(key)
    for area, keys in sites_by_area.items():
        if len(keys) > 1:
            for key in keys:
                area_by_site[key] = f"{area}_site{key}"
    return area_by_site


def _is_physical_ifname(ifname: str) -> bool:
    low = (ifname or "").strip().lower()
    return any(low.startswith(p) for p in _PHYSICAL_PREFIXES)


def _is_tunnel_ifname(ifname: str) -> bool:
    return (ifname or "").strip().lower().startswith("tunnel")


def is_loopback_ifname(ifname: str) -> bool:
    """True for a Loopback interface (``Loopback0``, ``Lo10``, ...).

    Public because the Overlay mapper must treat a Service VPN Loopback
    differently from a physical port: Network Sketcher's RULE 15 lists
    Loopback as an explicit exception to SVI/L2-segment binding, and every
    converter in this repo models a Loopback as an UNCONNECTED virtual port
    (``add virtual_port_bulk`` + ``add ip_address_bulk``) -- never as an L1
    link endpoint.
    """
    low = (ifname or "").strip().lower()
    return low.startswith("loopback") or low.startswith("lo") and low[2:3].isdigit()


def _vpn0(itf: Dict[str, Any]) -> bool:
    vpn = itf.get("vpn-id")
    try:
        return int(vpn) == 0
    except (TypeError, ValueError):
        return str(vpn).strip() == "0"


def vpn_id_of(itf: Dict[str, Any]) -> Optional[int]:
    """This interface's ``vpn-id`` as an int, or None when absent/unparseable.

    vManage is inconsistent here: ``/dataservice/device/interface`` reports it
    as a STRING (``"11"``) while the feature-template API reports the same id
    as an INTEGER (``10``), so every comparison in this converter goes through
    this normalisation.
    """
    raw = itf.get("vpn-id")
    if raw is None:
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def physical_wan_interfaces(interfaces: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """VPN0 physical interfaces carrying a real IPv4 address (Underlay ports).

    Excludes admin-down/unconfigured ports (``0.0.0.0`` or empty IP) and any
    non-physical VPN0 pseudo-interface (NVI0, Sdwan-system-intf,
    vmanage_system, Loopback*, Tunnel*).
    """
    out: List[Dict[str, Any]] = []
    for itf in interfaces or []:
        if not _vpn0(itf):
            continue
        ifname = itf.get("ifname") or itf.get("interface") or ""
        if not _is_physical_ifname(ifname):
            continue
        ip = (itf.get("ip-address") or "").strip()
        if not ip or ip == "0.0.0.0":
            continue
        out.append(itf)
    return out


def tunnel_wan_interfaces(interfaces: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """VPN0 Tunnel interfaces carrying a real IPv4 address.

    CURRENTLY UNUSED. It backed the previous per-transport-colour Overlay,
    which has been replaced by the Service VPN Overlay
    (``service_vpn_interfaces`` below); a VPN0 Tunnel interface is now drawn
    in NEITHER diagram. Kept because it is the single canonical definition of
    "a real SD-WAN TLOC-sourcing Tunnel interface" for this converter, should
    a future feature need it again.
    """
    out: List[Dict[str, Any]] = []
    for itf in interfaces or []:
        if not _vpn0(itf):
            continue
        ifname = itf.get("ifname") or itf.get("interface") or ""
        if not _is_tunnel_ifname(ifname):
            continue
        ip = (itf.get("ip-address") or "").strip()
        if not ip or ip == "0.0.0.0":
            continue
        out.append(itf)
    return out


def service_vpn_interfaces(
    interfaces: List[Dict[str, Any]],
    excluded_ids: Optional[Set[int]] = None,
) -> List[Tuple[int, Dict[str, Any]]]:
    """Service VPN (VRF) interfaces carrying a real IPv4 address (Overlay).

    Returns ``[(vpn_id, interface), ...]`` for every interface whose
    ``vpn-id``:

      * parses as an integer,
      * is NOT in :data:`RESERVED_VPN_IDS` (0 / 512 / 65528-65530 -- VPN 0 is
        the Transport VPN and belongs to the Underlay, and is therefore
        ALWAYS excluded regardless of configuration), and
      * is NOT in ``excluded_ids`` (the ``excluded_service_vpns``
        configuration value, already normalised to ints by the caller);

    and which is a real PHYSICAL port (``Gi``/``Te``/... , including dot1q
    sub-interfaces) or a Loopback, with a usable IPv4 address (not empty and
    not ``0.0.0.0``). Other pseudo/system interface types are skipped, since
    the Overlay has no meaningful way to draw them.

    The caller distinguishes the two accepted kinds with
    :func:`is_loopback_ifname`: a physical port becomes an L1 link into its
    VPN's waypoint cloud, whereas a Loopback becomes an unconnected virtual
    port (NS RULE 15's Loopback exception).
    """
    excluded = set(excluded_ids or ())
    out: List[Tuple[int, Dict[str, Any]]] = []
    for itf in interfaces or []:
        vpn_id = vpn_id_of(itf)
        if vpn_id is None or vpn_id in RESERVED_VPN_IDS or vpn_id in excluded:
            continue
        ifname = itf.get("ifname") or itf.get("interface") or ""
        if not (_is_physical_ifname(ifname) or is_loopback_ifname(ifname)):
            continue
        ip = (itf.get("ip-address") or "").strip()
        if not ip or ip == "0.0.0.0":
            continue
        out.append((vpn_id, itf))
    return out


def resolve_vpn_label(
    vpn_id: int,
    vpn_names: Dict[str, str],
    fallback_fmt: str = "VPN {id}",
) -> str:
    """Human-readable label for a Service VPN id.

    ``vpn_names`` maps a STRING vpn-id (``"10"``) to the VPN name configured
    in vManage's feature templates (``"Corporate"``) -- see
    ``fetch_from_vmanage.fetch_vpn_names``. That data is a TEMPLATE setting,
    not device running-config, so it is frequently unavailable (untemplated
    devices, Config-Group-managed fabrics, an older export); in that case
    ``fallback_fmt`` is formatted with ``id`` and returned instead.
    """
    name = sanitize(str((vpn_names or {}).get(str(vpn_id), "") or ""))
    if name:
        return name
    try:
        return sanitize(fallback_fmt.format(id=vpn_id)) or f"VPN {vpn_id}"
    except (IndexError, KeyError, ValueError):
        return f"VPN {vpn_id}"


DEFAULT_VRF_NAME_FORMAT = "{name}_vpn{id}"


def vrf_label_for_vpn(
    vpn_id: int,
    vpn_names: Dict[str, str],
    fmt: str = DEFAULT_VRF_NAME_FORMAT,
) -> str:
    """L3-instance (VRF) name for a Service VPN id.

    Deliberately DIFFERENT from :func:`resolve_vpn_label`, which names the
    waypoint cloud and its L2 segment: a VRF name has to stay unambiguous in
    the L3 interface table, so it always carries the numeric vpn-id.

      * with a configured VPN name -> ``fmt`` formatted with ``name`` and
        ``id`` (default ``Corporate_vpn10``);
      * without one -> plain ``vpn<id>`` (``vpn12``), NOT the
        ``vpn_name_fallback_format`` label -- ``VPN 12_vpn12`` would be both
        redundant and ugly.
    """
    name = sanitize(str((vpn_names or {}).get(str(vpn_id), "") or ""))
    if not name:
        return f"vpn{vpn_id}"
    try:
        label = sanitize(fmt.format(name=name, id=vpn_id))
    except (IndexError, KeyError, ValueError):
        label = ""
    return label or f"{name}_vpn{vpn_id}"


# ---------------------------------------------------------------------------
# LLDP / CDP neighbor record parsing -- UNVERIFIED SCHEMA, best-effort only.
#
# No confirmed real vManage ``/dataservice/device/lldp/neighbors`` or
# ``/dataservice/device/cdp/neighbors`` JSON response was obtainable while
# building this feature: the one live vManage this repo could reach (a
# CML-simulated lab) returns HTTP 404 for both, neither path appears in that
# platform's on-box Swagger spec (``/apidocs``), and Cisco's public "Device
# Realtime Monitoring" API reference documents no LLDP/CDP monitoring
# endpoint either. The field-name lists below are therefore a BEST-EFFORT
# default built from Cisco's own well-established vocabulary for this data --
# the same one NX-OS/IOS-XE ``show lldp/cdp neighbors`` JSON and this repo's
# ``catc_converter`` (Catalyst Center's LLDP-based physical-topology) already
# use (``local-interface`` / ``system-name`` / ``chassis-id`` / ``port-id`` /
# ``management-ip``), each with several plausible camelCase/kebab-case/
# NX-API-style variants so a real deployment's actual field names have the
# best chance of matching. VERIFY against a real physical vManage's actual
# response the first time this feature is used there (see the README).
# ---------------------------------------------------------------------------

_LOCAL_PORT_KEYS = (
    "local-interface", "localInterface", "local-intf", "local_interface",
    "local-port", "localPort", "l_intf_id", "ifname", "interface",
)
_REMOTE_PORT_KEYS = (
    "port-id", "portId", "remote-port-id", "remotePortId", "neighbor-port-id",
    "neighborPortId", "port_id", "l_port_id", "remote-interface",
    "remoteInterface", "remote-intf", "remote-port", "remotePort",
)
_REMOTE_SYSTEM_NAME_KEYS = (
    "system-name", "systemName", "sys-name", "sysName", "remote-system-name",
    "remoteSystemName", "host-name", "hostName", "device-id", "deviceId",
    "neighbor-system-name",
)
_REMOTE_CHASSIS_ID_KEYS = (
    "chassis-id", "chassisId", "chassis_id", "remote-chassis-id",
    "remoteChassisId", "device-id", "deviceId",
)
_REMOTE_MGMT_IP_KEYS = (
    "management-ip", "managementIp", "management-address", "managementAddress",
    "mgmt-address", "mgmtAddress", "mgmt-ip", "mgmt_addr", "mgmtAddr",
    "ip-address", "ipAddress",
)


def _first_nonempty(entry: Dict[str, Any], keys: tuple) -> str:
    """Try each plausible key name in order; return the first non-empty
    stripped string value found, else ''."""
    for k in keys:
        v = entry.get(k)
        if v not in (None, ""):
            s = str(v).strip()
            if s:
                return s
    return ""


def neighbor_local_port(entry: Dict[str, Any]) -> str:
    """Local (this device's) interface reported by an LLDP/CDP neighbor entry."""
    return _first_nonempty(entry, _LOCAL_PORT_KEYS)


def neighbor_remote_port(entry: Dict[str, Any]) -> str:
    """Neighbor-reported remote port/interface identifier."""
    return _first_nonempty(entry, _REMOTE_PORT_KEYS)


def neighbor_remote_system_name(entry: Dict[str, Any]) -> str:
    """Neighbor-reported remote system name / hostname / device-id."""
    return _first_nonempty(entry, _REMOTE_SYSTEM_NAME_KEYS)


def neighbor_remote_chassis_id(entry: Dict[str, Any]) -> str:
    """Neighbor-reported remote chassis id / device-id."""
    return _first_nonempty(entry, _REMOTE_CHASSIS_ID_KEYS)


def neighbor_remote_management_ip(entry: Dict[str, Any]) -> str:
    """Neighbor-reported remote management IP address."""
    return _first_nonempty(entry, _REMOTE_MGMT_IP_KEYS)


def resolve_neighbor_device(
    entry: Dict[str, Any],
    sysip_to_name: Dict[str, str],
    host_by_lower: Dict[str, str],
) -> Optional[str]:
    """Best-effort join of an LLDP/CDP neighbor entry's reported remote
    identity to an already-known SD-WAN-managed device's NS name.

    Tries, in order: remote system-name/host-name (case-insensitive against
    every managed device's raw ``host-name``), remote chassis-id (against
    both raw host-name and ``system-ip`` -- some platforms report a
    network-address chassis-id subtype equal to the system IP), then remote
    management-ip (against ``system-ip``). Returns None when no join key
    resolves to a known device (e.g. the neighbor is a real but unmanaged/
    non-SD-WAN device -- out of scope for this join, see the README).
    """
    name = neighbor_remote_system_name(entry)
    if name:
        hit = host_by_lower.get(name.lower())
        if hit:
            return hit
        hit = sysip_to_name.get(name)
        if hit:
            return hit
    chassis = neighbor_remote_chassis_id(entry)
    if chassis:
        hit = host_by_lower.get(chassis.lower()) or sysip_to_name.get(chassis)
        if hit:
            return hit
    mgmt_ip = neighbor_remote_management_ip(entry)
    if mgmt_ip:
        hit = sysip_to_name.get(mgmt_ip)
        if hit:
            return hit
    return None


def color_for_ip(wan_interfaces: List[Dict[str, Any]], ip: str) -> Optional[str]:
    """Resolve the transport ``color`` for an IP by matching it against this
    device's ``control/waninterface`` entries (private-ip or public-ip).

    A Tunnel interface and its parent physical (VPN0) interface share the same
    IP in vManage, so the same lookup resolves both.
    """
    ip = (ip or "").strip()
    if not ip:
        return None
    for w in wan_interfaces or []:
        if ip in (str(w.get("private-ip") or "").strip(), str(w.get("public-ip") or "").strip()):
            color = (w.get("color") or "").strip()
            if color:
                return color
    return None


def cidr_from_mask(ip: str, mask: str) -> Optional[str]:
    """Build a ``'<ip>/<prefixlen>'`` CIDR from an IPv4 address + dotted or
    prefix-length mask. Returns None on any parse failure."""
    ip = (ip or "").strip()
    mask = (mask or "").strip()
    if not ip or not mask:
        return None
    try:
        if "." in mask:
            prefix = ipaddress.IPv4Network(f"0.0.0.0/{mask}").prefixlen
        else:
            prefix = int(mask)
        ipaddress.IPv4Address(ip)  # validate the address itself
    except (ValueError, TypeError):
        return None
    if not 0 <= prefix <= 32:
        return None
    return f"{ip}/{prefix}"


def network_of(cidr: str) -> Optional[str]:
    """Return the network address (e.g. '172.16.1.0/24') for a host CIDR."""
    try:
        return str(ipaddress.ip_network(cidr, strict=False))
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Controller (vManage / vSmart / vBond) VPN0 interfaces + control-plane
# connectivity -- UNDERLAY only.
#
# VIPTELA-OS vs IOS-XE SCHEMA SPLIT: /dataservice/device/interface answers with
# two different shapes depending on the queried platform's OS, and a controller
# is always Viptela-OS even in a fabric whose Edges are all IOS-XE cEdges:
#
#   IOS-XE (cEdge)   {"ifname": "GigabitEthernet1", "ip-address": "172.16.1.1",
#                     "ipv4-subnet-mask": "255.255.255.0"}
#   Viptela-OS       {"ifname": "eth1",  "ip-address": "172.16.0.1/24"}
#                    {"ifname": "ge0/0", "ip-address": "172.16.0.201/24"}
#
# i.e. Viptela-OS uses short interface names (``eth1``, ``ge0/0``) and reports
# the prefix length INSIDE ``ip-address`` with no ``ipv4-subnet-mask`` field at
# all. ``_PHYSICAL_PREFIXES`` and ``cidr_from_mask`` above both assume the
# IOS-XE shape, so the two helpers below accept either -- deliberately scoped
# to the controller path rather than loosening the Edge filters.
# ---------------------------------------------------------------------------

CONTROLLER_PERSONALITIES = frozenset({"vmanage", "vsmart", "vbond"})

_VIPTELA_IFNAME_RE = re.compile(r"^(?:eth|ge)\d", re.IGNORECASE)


def interface_cidr(itf: Dict[str, Any]) -> Optional[str]:
    """This interface's ``'<ip>/<prefixlen>'``, tolerating BOTH schemas above.

    Prefers an ``ip-address`` that already carries the prefix (Viptela-OS),
    falling back to :func:`cidr_from_mask` with the separate
    ``ipv4-subnet-mask`` field (IOS-XE). Returns None when neither yields a
    usable address.
    """
    ip = str(itf.get("ip-address") or "").strip()
    if not ip:
        return None
    if "/" in ip:
        try:
            iface = ipaddress.ip_interface(ip)
        except (ValueError, TypeError):
            return None
        return f"{iface.ip}/{iface.network.prefixlen}"
    return cidr_from_mask(ip, str(itf.get("ipv4-subnet-mask") or ""))


def interface_ip(itf: Dict[str, Any]) -> str:
    """This interface's bare IPv4 address, with any prefix length stripped."""
    ip = str(itf.get("ip-address") or "").strip()
    return ip.split("/", 1)[0] if ip else ""


def controller_vpn0_interfaces(interfaces: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """VPN0 interfaces of a CONTROLLER that carry a real IPv4 address.

    Same intent as :func:`physical_wan_interfaces` (real routed VPN0 ports
    only, no ``0.0.0.0``/unconfigured port, no Tunnel/Loopback pseudo-port)
    but additionally accepting the Viptela-OS short names documented above.
    A controller's VPN 512 out-of-band management port is excluded by the VPN0
    filter, as it should be: that network is genuinely separate from the
    transport fabric.
    """
    out: List[Dict[str, Any]] = []
    for itf in interfaces or []:
        if not _vpn0(itf):
            continue
        ifname = str(itf.get("ifname") or itf.get("interface") or "")
        if not (_is_physical_ifname(ifname) or _VIPTELA_IFNAME_RE.match(ifname.strip())):
            continue
        if _is_tunnel_ifname(ifname) or is_loopback_ifname(ifname):
            continue
        ip = interface_ip(itf)
        if not ip or ip == "0.0.0.0":
            continue
        out.append(itf)
    return out


def control_connection_peer_type(row: Dict[str, Any]) -> str:
    """The PEER's role on a ``/dataservice/device/control/connections`` row
    read from an Edge (``vsmart`` / ``vmanage`` / ``vbond``)."""
    return str(row.get("peer-type") or "").strip().lower()


def control_connection_peer_sysip(row: Dict[str, Any]) -> str:
    """The PEER controller's ``system-ip``, or '' when unusable.

    A torn-down connection recorded in
    ``/dataservice/device/control/connectionshistory`` reports ``0.0.0.0``
    here (observed for every vBond row), which is never joinable and is
    normalised to '' so the caller falls back to the address join.
    """
    sysip = str(row.get("system-ip") or "").strip()
    return "" if sysip in ("", "0.0.0.0") else sysip


def control_connection_local_color(row: Dict[str, Any]) -> str:
    """The LOCAL (Edge-side) transport colour this control connection uses."""
    return str(row.get("local-color") or "").strip()


def control_connection_peer_addresses(row: Dict[str, Any]) -> List[str]:
    """The PEER controller's transport addresses, private first.

    They are equal on a NAT-free fabric but routinely differ in production
    (a NATed Edge or a cloud-hosted controller), so both are returned and the
    caller tries each in turn.
    """
    out: List[str] = []
    for key in ("private-ip", "public-ip"):
        value = str(row.get(key) or "").strip()
        if value and value != "0.0.0.0" and value not in out:
            out.append(value)
    return out


def control_connection_is_up(row: Dict[str, Any]) -> bool:
    """True when this control connection's reported ``state`` is ``up``.

    A row with no ``state`` field at all is treated as up: the field is
    present on every response observed so far, and dropping an otherwise
    complete row over a missing status field would lose real connectivity.
    """
    state = row.get("state")
    if state is None:
        return True
    return str(state).strip().lower() == "up"


# ---------------------------------------------------------------------------
# BFD session record parsing + UNDERLAY transport-segment mesh-shape
# classification.
#
# Unlike the LLDP/CDP neighbor collections above, ``/dataservice/device/bfd/
# sessions`` WAS reached and its field names WERE verified against a live
# vManage while implementing this feature -- a sample record looks like::
#
#     {"src-ip": "172.16.1.1", "dst-ip": "172.16.1.3", "color": "biz-internet",
#      "system-ip": "10.0.0.3", "site-id": 2, "state": "up", "proto": "ipsec"}
#
# i.e. each Edge's OWN session list reports, per BFD peer, the REMOTE peer's
# ``system-ip``/``site-id`` (not its own) plus the shared transport ``color``
# and the live session ``state`` -- no UNVERIFIED best-effort key list is
# needed here (contrast with the neighbor-parsing helpers above).
# ---------------------------------------------------------------------------

MESH_FULL = "Full Mesh"
MESH_HUB_SPOKE = "Hub-and-Spoke"
MESH_PARTIAL = "Partial Mesh"


def bfd_session_color(entry: Dict[str, Any]) -> str:
    """This BFD session's transport colour."""
    return (entry.get("color") or "").strip()


def bfd_session_remote_sysip(entry: Dict[str, Any]) -> str:
    """The REMOTE peer's ``system-ip`` reported by this (local) BFD session
    record -- NOT the local device's own system-ip."""
    return str(entry.get("system-ip") or "").strip()


def bfd_session_is_up(entry: Dict[str, Any]) -> bool:
    """True when this BFD session's observed ``state`` is ``up``."""
    return (entry.get("state") or "").strip().lower() == "up"


def classify_mesh_shape(
    edge_sysips: List[str],
    edge_site: Dict[str, str],
    bfd_sessions: Dict[str, List[Dict[str, Any]]],
    color: str,
) -> Optional[str]:
    """Classify ONE transport segment's SD-WAN fabric mesh shape --
    ``Full Mesh`` / ``Hub-and-Spoke`` / ``Partial Mesh`` -- from OBSERVED
    live BFD tunnel-session state, across the Edges given in ``edge_sysips``.

    Used by ``sdwan_physical_mapper`` to ANNOTATE each inferred UNDERLAY
    transport cloud, which is named after the colour alone ("biz-internet"):
    the cloud is the transport, and the annotation says how densely the SD-WAN
    fabric actually meshes across the Edges attached to it. No link is ever
    drawn from this.

    ``edge_sysips``: the ``system-ip`` of every Edge that is a MEMBER of the
    cloud being annotated. Since the clouds merged by colour alone, that is
    every Edge on the colour -- but the parameter stays member-scoped so the
    caller, not this function, decides what a cloud is.
    ``edge_site``: ``system-ip -> site-id``, used only to EXCLUDE same-site
    Edge pairs from the "should be connected" set below -- matching this
    tool's live validation environment, where two Edges at the same site-id
    did not form a colour-specific BFD adjacency to each other (they reach
    each other over the LAN side instead).
    ``bfd_sessions``: raw per-device ``SdwanIndex.bfd_sessions`` (a device's
    ``system-ip`` -> its own list of BFD session records).

    Returns ``None`` -- meaning "skip, do not annotate" -- when there is
    fewer than 2 relevant Edges, when NONE of them have ANY BFD session data
    at all (e.g. the bundled synthetic sample, or an export from
    ``fetch_from_vmanage.py`` predating this feature), or when the
    site-exclusion rule leaves no pair to evaluate. This mirrors the
    LLDP/CDP-based L1-link inference's own "no data -> no annotation"
    fallback policy.
    """
    peer_set: Set[str] = {ip for ip in edge_sysips if ip}
    if len(peer_set) < 2:
        return None

    any_data = False
    observed_pairs: Set[Tuple[str, str]] = set()
    for sysip in peer_set:
        for entry in bfd_sessions.get(sysip, []) or []:
            any_data = True
            if bfd_session_color(entry) != color or not bfd_session_is_up(entry):
                continue
            remote = bfd_session_remote_sysip(entry)
            if remote and remote in peer_set and remote != sysip:
                observed_pairs.add(tuple(sorted((sysip, remote))))
    if not any_data:
        return None

    expected_pairs: Set[Tuple[str, str]] = set()
    peers = sorted(peer_set)
    for i, a in enumerate(peers):
        for b in peers[i + 1:]:
            if edge_site.get(a) and edge_site.get(a) == edge_site.get(b):
                continue
            expected_pairs.add((a, b))
    if not expected_pairs:
        return None

    if observed_pairs >= expected_pairs:
        return MESH_FULL

    # Hub-and-Spoke: a SMALL set of "hub" Edges each connect to every OTHER
    # peer (degree == max possible), while every "spoke" (non-hub) Edge
    # connects ONLY to hub(s) -- never directly to another spoke. This is
    # judged purely from the OBSERVED graph shape, NOT against
    # ``expected_pairs`` (the full-mesh "every distinct site should connect"
    # expectation used above) -- a genuine hub-and-spoke topology, by
    # definition, never satisfies that full-mesh expectation, since spokes
    # deliberately do not connect to each other.
    degree: Dict[str, int] = defaultdict(int)
    for a, b in observed_pairs:
        degree[a] += 1
        degree[b] += 1
    max_possible = len(peers) - 1
    hubs = {p for p in peers if degree.get(p, 0) == max_possible}
    spokes = peer_set - hubs
    if hubs and spokes:
        spoke_to_spoke = any(a in spokes and b in spokes for a, b in observed_pairs)
        all_spokes_reach_all_hubs = all(
            tuple(sorted((s, h))) in observed_pairs for s in spokes for h in hubs
        )
        if not spoke_to_spoke and all_spokes_reach_all_hubs:
            return MESH_HUB_SPOKE

    return MESH_PARTIAL


_SANITIZE_RE = re.compile(r"\s+")


def sanitize(name: str) -> str:
    """Keep NS-safe characters (no quotes / brackets). Collapse whitespace."""
    if name is None:
        return ""
    keep = [ch for ch in str(name).strip() if ch.isalnum() or ch in " -_.+/()"]
    return _SANITIZE_RE.sub(" ", "".join(keep)).strip()
