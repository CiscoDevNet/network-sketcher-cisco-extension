# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""Ingest a Cisco Catalyst Center (DNA Center) export and index it.

``fetch_from_catc.py`` writes the model as a single *combined JSON* with this
shape (each Catalyst Center payload already unwrapped from its ``{"response":
...}`` envelope by the fetcher)::

    {
      "_meta": {"source": "Cisco Catalyst Center / DNA Center", "host": "..."},
      "devices":    [ <network-device>, ... ],
      "interfaces": { "<deviceId>": [ <interface>, ... ] },   # only with --with-interfaces
      "physicalTopology": {"nodes": [...], "links": [...]},
      "sites":       [ <site>, ... ],
      "fabricSites":  [ <fabricSite>, ... ],
      "fabricDevices":[ <fabricDevice>, ... ],
      "layer3VirtualNetworks": [ <l3vn>, ... ],
      "anycastGateways":       [ <anycastGateway>, ... ],
      "clients":     [ <client>, ... ]                        # only with --with-endpoints
    }

This reader collapses any supported input shape into a single :class:`CatcIndex`:

  * a single combined ``*.json`` file (the validated path),
  * a directory of ``*.json`` files (each a combined doc or a per-endpoint dump
    whose filename hints the collection, e.g. ``devices.json``), or
  * a ``.tar.gz`` / ``.tgz`` of the above.

Multiple documents are merged (lists concatenated, de-duplicated by a natural
key) so a split export still indexes cleanly.
"""
from __future__ import annotations

import json
import pathlib
import re
import sys
import tarfile
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# Top-level list collections carried in the combined JSON.
_LIST_KEYS = ("devices", "topoNodes", "topoLinks", "sites", "fabricSites",
              "fabricDevices", "layer3VirtualNetworks", "anycastGateways", "clients")

# Filename-prefix → collection key, for a directory of per-endpoint dumps.
_FILENAME_HINTS = {
    "device": "devices", "networkdevice": "devices", "inventory": "devices",
    "interface": "interfaces",
    "physicaltopology": "physicalTopology", "topology": "physicalTopology",
    "site": "sites",
    "fabricsite": "fabricSites",
    "fabricdevice": "fabricDevices",
    "layer3virtualnetwork": "layer3VirtualNetworks", "l3vn": "layer3VirtualNetworks",
    "virtualnetwork": "layer3VirtualNetworks",
    "anycastgateway": "anycastGateways",
    "client": "clients", "host": "clients", "endpoint": "clients",
}


@dataclass
class CatcIndex:
    """Indexed Catalyst Center model: devices + topology + SD-Access overlay."""
    devices: List[Dict[str, Any]] = field(default_factory=list)
    device_by_id: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    device_by_ip: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    interfaces: Dict[str, List[Dict[str, Any]]] = field(default_factory=lambda: defaultdict(list))
    topo_nodes: List[Dict[str, Any]] = field(default_factory=list)
    topo_links: List[Dict[str, Any]] = field(default_factory=list)
    sites: List[Dict[str, Any]] = field(default_factory=list)
    fabric_sites: List[Dict[str, Any]] = field(default_factory=list)
    fabric_devices: List[Dict[str, Any]] = field(default_factory=list)
    l3_vns: List[Dict[str, Any]] = field(default_factory=list)
    anycast_gateways: List[Dict[str, Any]] = field(default_factory=list)
    clients: List[Dict[str, Any]] = field(default_factory=list)
    ssids: List[Dict[str, Any]] = field(default_factory=list)
    wireless_profiles: List[Dict[str, Any]] = field(default_factory=list)

    def site_by_id(self) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for s in self.sites:
            sid = s.get("id")
            if sid:
                out[str(sid)] = s
        return out

    def reindex(self) -> None:
        """Rebuild device_by_id / device_by_ip from the devices list."""
        self.device_by_id = {}
        self.device_by_ip = {}
        for d in self.devices:
            did = d.get("id")
            if did:
                self.device_by_id[str(did)] = d
            ip = d.get("managementIpAddress")
            if ip:
                self.device_by_ip[str(ip)] = d

    def summary(self) -> Dict[str, int]:
        return {
            "devices": len(self.devices),
            "links": len(self.topo_links),
            "fabricSites": len(self.fabric_sites),
            "vns": len(self.l3_vns),
            "anycastGateways": len(self.anycast_gateways),
            "clients": len(self.clients),
            "interfaces": sum(len(v) for v in self.interfaces.values()),
            "ssids": len(self.ssids),
        }


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_export(path: pathlib.Path) -> CatcIndex:
    """Load a Catalyst Center export from a JSON file, directory, or tar.gz."""
    docs = _read_json_documents(path)
    idx = CatcIndex()
    for name, doc in docs:
        _merge_document(idx, name, doc)
    _dedupe(idx)
    idx.reindex()
    return idx


def _read_json_documents(path: pathlib.Path) -> List[Any]:
    """Return [(source_name, parsed_doc), ...] from any supported input shape."""
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

    if tarfile.is_tarfile(path):
        with tarfile.open(path, "r:*") as tar:
            members = [m for m in tar.getmembers() if m.isfile()]
            json_members = [m for m in members if m.name.lower().endswith(".json")]
            if not json_members:
                raise ValueError(
                    f"archive {path} contains no *.json members "
                    f"(found {len(members)} file(s))."
                )
            for m in json_members:
                fh = tar.extractfile(m)
                if fh is None:
                    continue
                try:
                    docs.append((m.name, json.loads(fh.read().decode("utf-8", errors="replace"))))
                except json.JSONDecodeError as exc:
                    print(f"[WARN] skipping unparseable JSON member {m.name}: {exc}",
                          file=sys.stderr)
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


def _unwrap(block: Any) -> Any:
    """Unwrap a Catalyst Center ``{"response": ...}`` envelope (best effort)."""
    if isinstance(block, dict) and "response" in block and len(block) <= 2:
        return block["response"]
    return block


def _merge_document(idx: CatcIndex, source_name: str, doc: Any) -> None:
    """Merge one parsed document into the index.

    Recognised shapes:
      * a combined dict with ``devices`` / ``physicalTopology`` / ... keys,
      * a bare list whose filename hints the collection (per-endpoint dump),
      * a bare dict that is a single Catalyst Center ``{"response": ...}``
        envelope (filename hints the collection).
    """
    if isinstance(doc, dict) and _looks_combined(doc):
        _merge_combined(idx, doc)
        return

    # A per-endpoint dump: infer the collection from the filename.
    coll = _infer_from_filename(source_name)
    payload = _unwrap(doc)
    if coll:
        _merge_collection(idx, coll, payload)
        return

    # Unknown dict that is just a {"response": ...} envelope of a combined doc.
    if isinstance(doc, dict):
        inner = _unwrap(doc)
        if isinstance(inner, dict) and _looks_combined(inner):
            _merge_combined(idx, inner)


def _merge_combined(idx: CatcIndex, doc: Dict[str, Any]) -> None:
    for key in ("devices", "sites", "fabricSites", "fabricDevices",
                "layer3VirtualNetworks", "anycastGateways", "clients",
                "ssids", "wirelessProfiles"):
        block = _unwrap(doc.get(key))
        if isinstance(block, list):
            _merge_collection(idx, key, block)
    pt = _unwrap(doc.get("physicalTopology"))
    if isinstance(pt, dict):
        _merge_collection(idx, "physicalTopology", pt)
    itf = doc.get("interfaces")
    if isinstance(itf, dict):
        for did, items in itf.items():
            block = _unwrap(items)
            if isinstance(block, list):
                idx.interfaces[str(did)].extend(i for i in block if isinstance(i, dict))


def _merge_collection(idx: CatcIndex, key: str, payload: Any) -> None:
    """Merge a single collection payload (already unwrapped) into the index."""
    if key == "physicalTopology":
        if isinstance(payload, dict):
            idx.topo_nodes.extend(n for n in (payload.get("nodes") or []) if isinstance(n, dict))
            idx.topo_links.extend(l for l in (payload.get("links") or []) if isinstance(l, dict))
        return
    if key == "interfaces":
        # A bare interfaces list with no device association: best effort by deviceId.
        for itf in (payload or []):
            if isinstance(itf, dict):
                did = str(itf.get("deviceId") or "")
                if did:
                    idx.interfaces[did].append(itf)
        return
    target = {
        "devices": idx.devices, "sites": idx.sites, "fabricSites": idx.fabric_sites,
        "fabricDevices": idx.fabric_devices, "layer3VirtualNetworks": idx.l3_vns,
        "anycastGateways": idx.anycast_gateways, "clients": idx.clients,
        "ssids": idx.ssids, "wirelessProfiles": idx.wireless_profiles,
    }.get(key)
    if target is None or not isinstance(payload, list):
        return
    target.extend(i for i in payload if isinstance(i, dict))


def _looks_combined(doc: Dict[str, Any]) -> bool:
    return any(k in doc for k in ("devices", "physicalTopology", "fabricSites",
                                  "layer3VirtualNetworks", "anycastGateways",
                                  "sites", "fabricDevices", "interfaces",
                                  "ssids", "wirelessProfiles"))


_FNAME_RE = re.compile(r"^([A-Za-z0-9]+)(?:[_-].*)?\.json$", re.IGNORECASE)


def _infer_from_filename(name: str) -> str:
    """('devices.json') -> 'devices'; ('fabric_devices.json') -> 'fabricDevices'."""
    base = name.split("/")[-1].split("\\")[-1]
    low = base.lower().replace("_", "").replace("-", "")
    m = _FNAME_RE.match(base)
    prefix = m.group(1).lower().replace("_", "").replace("-", "") if m else low
    # Try longest hints first so 'fabricdevice' beats 'device'.
    for hint in sorted(_FILENAME_HINTS, key=len, reverse=True):
        if low.startswith(hint) or prefix.startswith(hint):
            return _FILENAME_HINTS[hint]
    return ""


def _dedupe(idx: CatcIndex) -> None:
    """De-duplicate merged collections by a natural key (split exports overlap)."""

    def _dedupe_list(items: List[Dict[str, Any]], keyfn) -> List[Dict[str, Any]]:
        seen: set = set()
        uniq: List[Dict[str, Any]] = []
        for it in items:
            k = keyfn(it)
            if k is not None and k in seen:
                continue
            seen.add(k)
            uniq.append(it)
        return uniq

    idx.devices = _dedupe_list(idx.devices, lambda d: d.get("id") or d.get("managementIpAddress"))
    idx.sites = _dedupe_list(idx.sites, lambda s: s.get("id"))
    idx.fabric_sites = _dedupe_list(idx.fabric_sites, lambda f: f.get("id"))
    idx.fabric_devices = _dedupe_list(idx.fabric_devices, lambda f: f.get("id") or
                                      (f.get("fabricId"), f.get("networkDeviceId")))
    idx.l3_vns = _dedupe_list(idx.l3_vns, lambda v: v.get("id") or v.get("virtualNetworkName"))
    idx.anycast_gateways = _dedupe_list(
        idx.anycast_gateways, lambda g: g.get("id") or (g.get("fabricId"), g.get("vlanId")))
    idx.clients = _dedupe_list(idx.clients, lambda c: c.get("id") or c.get("macAddress"))
    idx.topo_nodes = _dedupe_list(idx.topo_nodes, lambda n: n.get("id"))
    idx.topo_links = _dedupe_list(
        idx.topo_links,
        lambda l: (l.get("source"), l.get("target"),
                   l.get("startPortName"), l.get("endPortName")))
