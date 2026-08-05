# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""Ingest a Cisco Catalyst SD-WAN Manager (vManage) export and index it.

``fetch_from_vmanage.py`` writes the model as a single *combined JSON* with
this shape::

    {
      "_meta": {"source": "Cisco SD-WAN Manager (vManage)", "host": "..."},
      "devices": [ <device>, ... ],
      "interfaces":     { "<system-ip>": [ <interface>, ... ] },
      "wan_interfaces": { "<system-ip>": [ <waninterface>, ... ] },
      "lldp_neighbors": { "<system-ip>": [ <lldp-neighbor>, ... ] },
      "cdp_neighbors":  { "<system-ip>": [ <cdp-neighbor>, ... ] },
      "bfd_sessions":   { "<system-ip>": [ <bfd-session>, ... ] },
      "control_connections":         { "<system-ip>": [ <control-connection>, ... ] },
      "control_connections_history": { "<system-ip>": [ <control-connection>, ... ] },
      "vpn_names":      { "<vpn-id>": "<vpn name>", ... }
    }

Each ``<device>`` is a raw vManage ``/dataservice/device`` record (at minimum
``host-name``, ``system-ip``, ``site-id``, ``device-model``, ``personality``,
``uuid``, ``reachability``), normally also including ``site-name`` -- the
human-readable site label (``site-id 3`` -> ``"br1"``) both mappers prefer
over ``site<id>`` as the NS area name (see
``sdwan_topology.build_site_area_map``); an export without it simply falls
back to ``site<id>``. Each ``<interface>`` is a raw
``/dataservice/device/interface`` record (``ifname``, ``vpn-id``,
``ip-address``, ``ipv4-subnet-mask``, ``if-admin-status``). NOTE: a real
vManage does NOT return an ``af-type`` field on these records at all (only
the bundled synthetic sample carries one); nothing in this converter filters
on it. Each ``<waninterface>`` is a raw
``/dataservice/device/control/waninterface`` record (``color``,
``private-ip``, ``public-ip``).

Service VPN membership is read exclusively from each interface record's
``vpn-id``. There is deliberately no VPN collection here:
``/dataservice/device/vpn`` was observed to always return an empty
``"data": []`` array and is therefore not a usable source.

``lldp_neighbors`` / ``cdp_neighbors`` are both OPTIONAL top-level
collections (absent, empty, or partially populated -- all handled the same
as a plain empty dict) holding whatever raw neighbor records
``fetch_from_vmanage.py`` could pull from ``/dataservice/device/lldp/neighbors``
/ ``/dataservice/device/cdp/neighbors`` for that device. **The exact field
names inside each neighbor record are UNVERIFIED against a real physical
vManage** (see ``sdwan_physical_mapper.py`` and the README's accuracy
caveats) -- this reader stores them verbatim and leaves all field-name
interpretation to the physical mapper's defensive parser. An export with
neither key (e.g. the bundled sample, or any older export) parses identically
to before this collection existed.

``bfd_sessions`` is another OPTIONAL top-level collection (same
absent/empty/partial handling) holding raw
``/dataservice/device/bfd/sessions`` records per Edge device's ``system-ip``.
Unlike the neighbor collections above, this endpoint's field names (``color``,
``system-ip`` of the remote peer, ``site-id``, ``state``) WERE verified
against a live vManage while implementing this feature, so
``sdwan_physical_mapper.py``'s mesh-shape annotation (Full Mesh /
Hub-and-Spoke / Partial Mesh) trusts them directly -- see ``sdwan_topology.py``.
An export without this key (e.g. the bundled sample) simply skips mesh-shape
classification, exactly like the neighbor collections skip L1-link inference.

``control_connections`` / ``control_connections_history`` are two more
OPTIONAL top-level collections (same absent/empty/partial handling) holding
raw ``/dataservice/device/control/[synced/]connections`` and
``/dataservice/device/control/connectionshistory`` records, keyed by the
system-ip of the device they were QUERIED FOR -- normally an Edge, whose rows
each describe one control connection UP to a controller (``peer-type``,
``system-ip`` of that controller, ``local-color`` = the Edge's own transport
colour, ``private-ip``/``public-ip`` = the controller's transport address,
``state``). ``sdwan_physical_mapper.py`` joins them to the transport segments
it already inferred, to draw the controllers' control-plane connectivity in
the UNDERLAY. Both endpoints REQUIRE a ``deviceId`` query parameter (a bare
call answers HTTP 400), so there is no fabric-wide variant to collect. The
history collection exists only because a vBond's connections are torn down
after onboarding and therefore appear nowhere else. An export without these
keys (e.g. any export predating this feature) draws the controllers as
unconnected inventory nodes, exactly as before.

``vpn_names`` is a final OPTIONAL top-level collection mapping a Service VPN
id to its configured name (``{"10": "Corporate"}``), resolved fabric-wide
from vManage's feature-template library by ``fetch_from_vmanage.py``. Keys
are normalised to STRINGS here, because vManage reports the same id as an
integer in the template API and as a string in the interface API. Without
it, ``sdwan_logical_mapper.py`` labels each Service VPN waypoint cloud
``VPN <id>`` instead.

This reader collapses either a single combined ``*.json`` file (the primary,
validated path) or a directory of ``*.json`` files (a combined doc, or one
per-endpoint dump per file, filename-hinted) into a single :class:`SdwanIndex`.
"""
from __future__ import annotations

import json
import pathlib
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class SdwanIndex:
    """Indexed vManage model: device inventory + per-device interface state."""
    devices: List[Dict[str, Any]] = field(default_factory=list)
    device_by_sysip: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    interfaces: Dict[str, List[Dict[str, Any]]] = field(default_factory=lambda: defaultdict(list))
    wan_interfaces: Dict[str, List[Dict[str, Any]]] = field(default_factory=lambda: defaultdict(list))
    # OPTIONAL -- absent entirely on any export that predates this feature (or
    # on a platform whose vManage does not expose the endpoint at all, e.g.
    # this repo's own bundled CML-lab sample). See the module docstring.
    lldp_neighbors: Dict[str, List[Dict[str, Any]]] = field(default_factory=lambda: defaultdict(list))
    cdp_neighbors: Dict[str, List[Dict[str, Any]]] = field(default_factory=lambda: defaultdict(list))
    # OPTIONAL -- same handling as lldp_neighbors/cdp_neighbors above, but the
    # field names inside each record ARE verified (see module docstring).
    bfd_sessions: Dict[str, List[Dict[str, Any]]] = field(default_factory=lambda: defaultdict(list))
    # OPTIONAL -- per-device (normally per-Edge) control-plane connections to
    # the controllers, current and torn-down. See the module docstring.
    control_connections: Dict[str, List[Dict[str, Any]]] = field(
        default_factory=lambda: defaultdict(list))
    control_connections_history: Dict[str, List[Dict[str, Any]]] = field(
        default_factory=lambda: defaultdict(list))
    # OPTIONAL -- Service VPN id (as a STRING, e.g. "10") -> configured VPN
    # name ("Corporate"). See the module docstring.
    vpn_names: Dict[str, str] = field(default_factory=dict)

    def reindex(self) -> None:
        self.device_by_sysip = {}
        for d in self.devices:
            sysip = d.get("system-ip")
            if sysip:
                self.device_by_sysip[str(sysip)] = d

    def summary(self) -> Dict[str, int]:
        return {
            "devices": len(self.devices),
            "interfaces": sum(len(v) for v in self.interfaces.values()),
            "wan_interfaces": sum(len(v) for v in self.wan_interfaces.values()),
            "lldp_neighbors": sum(len(v) for v in self.lldp_neighbors.values()),
            "cdp_neighbors": sum(len(v) for v in self.cdp_neighbors.values()),
            "bfd_sessions": sum(len(v) for v in self.bfd_sessions.values()),
            "control_connections": sum(len(v) for v in self.control_connections.values()),
            "control_connections_history": sum(
                len(v) for v in self.control_connections_history.values()),
            "vpn_names": len(self.vpn_names),
        }


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_export(path: pathlib.Path) -> SdwanIndex:
    """Load a vManage export from a single combined JSON file or a directory
    of ``*.json`` files."""
    docs = _read_json_documents(path)
    idx = SdwanIndex()
    for name, doc in docs:
        _merge_document(idx, name, doc)
    _dedupe(idx)
    idx.reindex()
    return idx


def _read_json_documents(path: pathlib.Path) -> List[Any]:
    if not path.exists():
        raise FileNotFoundError(f"input path not found: {path}")

    docs: List[Any] = []
    if path.is_dir():
        json_files = sorted(path.glob("**/*.json"))
        if not json_files:
            raise ValueError(f"no *.json files found under directory: {path}")
        for p in json_files:
            doc = _load_json_file(p)
            if doc is not None:
                docs.append((p.name, doc))
        return docs

    doc = _load_json_file(path)
    if doc is None:
        raise ValueError(f"could not parse JSON from {path}")
    docs.append((path.name, doc))
    return docs


def _load_json_file(p: pathlib.Path) -> Any:
    try:
        with p.open(encoding="utf-8-sig", errors="replace") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[WARN] skipping unparseable JSON file {p}: {exc}", file=sys.stderr)
        return None


def _looks_combined(doc: Dict[str, Any]) -> bool:
    return any(k in doc for k in (
        "devices", "interfaces", "wan_interfaces", "lldp_neighbors", "cdp_neighbors",
        "bfd_sessions", "control_connections", "control_connections_history",
        "vpn_names",
    ))


_FILENAME_HINTS = {
    "device": "devices", "interface": "interfaces", "waninterface": "wan_interfaces",
    "wan_interface": "wan_interfaces", "control": "wan_interfaces",
    # Longest-prefix wins (see _infer_from_filename), so these stay distinct
    # from the bare "control" -> wan_interfaces hint above.
    "control_connection": "control_connections",
    "connections": "control_connections",
    "control_connections_history": "control_connections_history",
    "control_connectionshistory": "control_connections_history",
    "connectionshistory": "control_connections_history",
    # A bare vpn_names.json / vpn-name.json holds {"10": "Corporate"} at the
    # top level, so _looks_combined cannot recognise it -- the filename is the
    # only hint available.
    "vpn_name": "vpn_names",
}

# Collections keyed by the system-ip of the device they were queried for, so a
# per-endpoint file can still be merged when it holds that same mapping.
_PER_DEVICE_COLLECTIONS = ("control_connections", "control_connections_history")


def _infer_from_filename(name: str) -> str:
    base = name.split("/")[-1].split("\\")[-1].lower()
    stem = base[:-5] if base.endswith(".json") else base
    stem = stem.replace("-", "_")
    for hint in sorted(_FILENAME_HINTS, key=len, reverse=True):
        if stem.startswith(hint):
            return _FILENAME_HINTS[hint]
    return ""


def _merge_document(idx: SdwanIndex, source_name: str, doc: Any) -> None:
    if isinstance(doc, dict) and _looks_combined(doc):
        _merge_combined(idx, doc)
        return
    coll = _infer_from_filename(source_name)
    if not coll:
        return
    if coll == "vpn_names":
        _merge_vpn_names(idx, doc)
        return
    if coll in _PER_DEVICE_COLLECTIONS:
        _merge_per_device(getattr(idx, coll), doc)
        return
    if coll == "devices" and isinstance(doc, dict):
        doc = doc.get("data", doc)
    if coll == "devices" and isinstance(doc, list):
        idx.devices.extend(d for d in doc if isinstance(d, dict))


def _merge_per_device(target: Dict[str, List[Dict[str, Any]]], blob: Any) -> None:
    """Merge a ``{system-ip: [record, ...]}`` mapping into ``target``."""
    if not isinstance(blob, dict):
        return
    for sysip, items in blob.items():
        if isinstance(items, list):
            target[str(sysip)].extend(i for i in items if isinstance(i, dict))


def _merge_vpn_names(idx: SdwanIndex, blob: Any) -> None:
    """Merge a ``{vpn-id: name}`` mapping, last-wins, with STRING keys.

    vManage's feature-template API reports ``vpn-id`` as an integer while
    ``/dataservice/device/interface`` reports it as a string, so both forms
    are normalised to a string key here (the mappers only ever look up by
    ``str(vpn_id)``)."""
    if not isinstance(blob, dict):
        return
    for vpn_id, name in blob.items():
        if vpn_id is None or name is None:
            continue
        key = str(vpn_id).strip()
        label = str(name).strip()
        if key and label:
            idx.vpn_names[key] = label


def _merge_combined(idx: SdwanIndex, doc: Dict[str, Any]) -> None:
    devices = doc.get("devices")
    if isinstance(devices, list):
        idx.devices.extend(d for d in devices if isinstance(d, dict))
    itf = doc.get("interfaces")
    if isinstance(itf, dict):
        for sysip, items in itf.items():
            if isinstance(items, list):
                idx.interfaces[str(sysip)].extend(i for i in items if isinstance(i, dict))
    wan = doc.get("wan_interfaces")
    if isinstance(wan, dict):
        for sysip, items in wan.items():
            if isinstance(items, list):
                idx.wan_interfaces[str(sysip)].extend(i for i in items if isinstance(i, dict))
    lldp = doc.get("lldp_neighbors")
    if isinstance(lldp, dict):
        for sysip, items in lldp.items():
            if isinstance(items, list):
                idx.lldp_neighbors[str(sysip)].extend(i for i in items if isinstance(i, dict))
    cdp = doc.get("cdp_neighbors")
    if isinstance(cdp, dict):
        for sysip, items in cdp.items():
            if isinstance(items, list):
                idx.cdp_neighbors[str(sysip)].extend(i for i in items if isinstance(i, dict))
    bfd = doc.get("bfd_sessions")
    if isinstance(bfd, dict):
        for sysip, items in bfd.items():
            if isinstance(items, list):
                idx.bfd_sessions[str(sysip)].extend(i for i in items if isinstance(i, dict))
    for coll in _PER_DEVICE_COLLECTIONS:
        _merge_per_device(getattr(idx, coll), doc.get(coll))
    _merge_vpn_names(idx, doc.get("vpn_names"))


def _dedupe(idx: SdwanIndex) -> None:
    seen: set = set()
    uniq: List[Dict[str, Any]] = []
    for d in idx.devices:
        key = d.get("uuid") or d.get("system-ip") or d.get("host-name")
        if key is not None and key in seen:
            continue
        seen.add(key)
        uniq.append(d)
    idx.devices = uniq
