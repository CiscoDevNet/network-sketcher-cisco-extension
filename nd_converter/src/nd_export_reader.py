# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""Ingest a Nexus Dashboard (NDFC / Fabric Controller) export and index it.

Unlike ACI's APIC Configuration Export (a ``polUni`` Managed-Information-Tree),
NDFC exposes its model as discrete REST collections — a list of fabrics, and per
fabric a list of switches, links, VRFs and Networks. ``fetch_from_nd.py`` writes
them as a single *combined JSON* with this shape::

    {
      "fabrics":  [ {"fabricName": "...", "fabricTechnology": "...", ...}, ... ],
      "switches": { "<fabricName>": [ <switch>, ... ] },
      "links":    { "<fabricName>": [ <link>,   ... ] },
      "vrfs":     { "<fabricName>": [ <vrf>,    ... ] },
      "networks": { "<fabricName>": [ <network>, ... ] },
      "vrfAttachments":     { "<fabricName>": [ ... ] },
      "networkAttachments": { "<fabricName>": [ ... ] }
    }

This reader collapses any supported input shape into a single :class:`NdIndex`:

  * a single combined ``*.json`` file (the validated path),
  * a directory of ``*.json`` files (each a combined doc or a per-endpoint dump
    whose filename hints the collection + fabric, e.g.
    ``switches_DevNet_VxLAN_Fabric.json``), or
  * a ``.tar.gz`` / ``.tgz`` of the above.

Multiple documents are merged (per-fabric lists are concatenated, de-duplicated
by a natural key) so a split export still indexes cleanly.
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


# Top-level collection keys carried in the combined JSON. Each maps fabricName
# -> list of objects (except ``fabrics`` which is a plain list).
#
# The first six are always produced by ``fetch_from_nd``; the rest are OPTIONAL
# richer sources (Endpoint Locator hosts, vPC pairs, interface detail, Multi-Site
# associations) that are consumed when present and silently ignored when absent —
# so a fabric that has them gets a richer diagram with no change to the others.
_PER_FABRIC_KEYS = ("switches", "links", "vrfs", "networks",
                    "vrfAttachments", "networkAttachments",
                    "endpoints", "vpcPairs", "interfaces", "msdAssociations")

# Filename-prefix → collection key, for a directory of per-endpoint dumps.
_FILENAME_HINTS = {
    "switch": "switches", "inventory": "switches",
    "link": "links", "topology": "links",
    "vrf": "vrfs", "network": "networks", "fabric": "fabrics",
    "endpoint": "endpoints", "host": "endpoints",
    "vpc": "vpcPairs", "interface": "interfaces", "msd": "msdAssociations",
}


@dataclass
class NdIndex:
    """Indexed NDFC model: fabrics + per-fabric switches / links / vrfs / networks."""
    fabrics: List[Dict[str, Any]] = field(default_factory=list)
    switches: Dict[str, List[Dict[str, Any]]] = field(default_factory=lambda: defaultdict(list))
    links: Dict[str, List[Dict[str, Any]]] = field(default_factory=lambda: defaultdict(list))
    vrfs: Dict[str, List[Dict[str, Any]]] = field(default_factory=lambda: defaultdict(list))
    networks: Dict[str, List[Dict[str, Any]]] = field(default_factory=lambda: defaultdict(list))
    vrf_attachments: Dict[str, List[Dict[str, Any]]] = field(default_factory=lambda: defaultdict(list))
    network_attachments: Dict[str, List[Dict[str, Any]]] = field(default_factory=lambda: defaultdict(list))
    # Optional richer sources (consumed when present, ignored when absent).
    endpoints: Dict[str, List[Dict[str, Any]]] = field(default_factory=lambda: defaultdict(list))
    vpc_pairs: Dict[str, List[Dict[str, Any]]] = field(default_factory=lambda: defaultdict(list))
    interfaces: Dict[str, List[Dict[str, Any]]] = field(default_factory=lambda: defaultdict(list))
    msd_associations: Dict[str, List[Dict[str, Any]]] = field(default_factory=lambda: defaultdict(list))

    def fabric_names(self) -> List[str]:
        """All fabric names, from the fabrics list plus any seen in collections."""
        names = {f.get("fabricName") for f in self.fabrics if f.get("fabricName")}
        for coll in (self.switches, self.links, self.vrfs, self.networks):
            names.update(coll.keys())
        return sorted(n for n in names if n)

    def fabric(self, name: str) -> Dict[str, Any]:
        for f in self.fabrics:
            if f.get("fabricName") == name:
                return f
        return {"fabricName": name}

    def summary(self) -> Dict[str, int]:
        return {
            "fabrics": len(self.fabric_names()),
            "switches": sum(len(v) for v in self.switches.values()),
            "links": sum(len(v) for v in self.links.values()),
            "vrfs": sum(len(v) for v in self.vrfs.values()),
            "networks": sum(len(v) for v in self.networks.values()),
            "endpoints": sum(len(v) for v in self.endpoints.values()),
            "vpcPairs": sum(len(v) for v in self.vpc_pairs.values()),
            "interfaces": sum(len(v) for v in self.interfaces.values()),
        }


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_export(path: pathlib.Path) -> NdIndex:
    """Load a Nexus Dashboard export from a JSON file, directory, or tar.gz."""
    docs = _read_json_documents(path)
    idx = NdIndex()
    for name, doc in docs:
        _merge_document(idx, name, doc)
    _dedupe(idx)
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


def _merge_document(idx: NdIndex, source_name: str, doc: Any) -> None:
    """Merge one parsed document into the index.

    Recognised shapes:
      * a combined dict with ``fabrics`` / ``switches`` / ... keys (preferred),
      * a bare list whose filename hints the collection + fabric (per-endpoint
        dump), or
      * a bare dict that itself is a single fabric / NDFC response envelope.
    """
    if isinstance(doc, dict) and _looks_combined(doc):
        if isinstance(doc.get("fabrics"), list):
            idx.fabrics.extend(f for f in doc["fabrics"] if isinstance(f, dict))
        for key in _PER_FABRIC_KEYS:
            block = doc.get(key)
            if isinstance(block, dict):
                target = _collection_for(idx, key)
                for fabric_name, items in block.items():
                    if isinstance(items, list):
                        target[fabric_name].extend(i for i in items if isinstance(i, dict))
        return

    # A per-endpoint dump: a bare list. Infer collection + fabric from filename.
    if isinstance(doc, list):
        coll, fabric_name = _infer_from_filename(source_name)
        items = [i for i in doc if isinstance(i, dict)]
        if coll == "fabrics":
            idx.fabrics.extend(items)
            return
        if coll and fabric_name:
            _collection_for(idx, coll)[fabric_name].extend(items)
            return
        # Unknown list: try to detect fabrics by their fields.
        if items and "fabricName" in items[0] and ("fabricTechnology" in items[0] or "templateName" in items[0]):
            idx.fabrics.extend(items)
        return

    # An NDFC response envelope {"data": [...]} / {"fabrics": [...]} single key.
    if isinstance(doc, dict):
        for k in ("data", "value"):
            if isinstance(doc.get(k), list):
                _merge_document(idx, source_name, doc[k])
                return


def _looks_combined(doc: Dict[str, Any]) -> bool:
    return any(k in doc for k in (("fabrics",) + _PER_FABRIC_KEYS))


def _collection_for(idx: NdIndex, key: str) -> Dict[str, List[Dict[str, Any]]]:
    return {
        "switches": idx.switches, "links": idx.links, "vrfs": idx.vrfs,
        "networks": idx.networks, "vrfAttachments": idx.vrf_attachments,
        "networkAttachments": idx.network_attachments,
        "endpoints": idx.endpoints, "vpcPairs": idx.vpc_pairs,
        "interfaces": idx.interfaces, "msdAssociations": idx.msd_associations,
    }[key]


_FNAME_RE = re.compile(r"^([A-Za-z]+)[_-](.+?)\.json$", re.IGNORECASE)


def _infer_from_filename(name: str) -> tuple:
    """('switches_DevNet.json') -> ('switches', 'DevNet'); ('fabrics.json') -> ('fabrics', '')."""
    base = name.split("/")[-1].split("\\")[-1]
    low = base.lower()
    if low in ("fabrics.json", "fabric.json"):
        return "fabrics", ""
    m = _FNAME_RE.match(base)
    if not m:
        return "", ""
    prefix, fabric = m.group(1).lower(), m.group(2)
    for hint, coll in _FILENAME_HINTS.items():
        if prefix.startswith(hint):
            return coll, ("" if coll == "fabrics" else fabric)
    return "", ""


def _dedupe(idx: NdIndex) -> None:
    """De-duplicate merged collections by a natural key (split exports overlap)."""
    seen_f: set = set()
    uniq_f: List[Dict[str, Any]] = []
    for f in idx.fabrics:
        k = f.get("fabricName")
        if k and k in seen_f:
            continue
        seen_f.add(k)
        uniq_f.append(f)
    idx.fabrics = uniq_f

    def _dedupe_coll(coll: Dict[str, List[Dict[str, Any]]], keyfn) -> None:
        for fabric, items in coll.items():
            seen: set = set()
            uniq: List[Dict[str, Any]] = []
            for it in items:
                k = keyfn(it)
                if k is not None and k in seen:
                    continue
                seen.add(k)
                uniq.append(it)
            coll[fabric] = uniq

    _dedupe_coll(idx.switches, lambda s: s.get("serialNumber") or s.get("ipAddress") or s.get("logicalName"))
    _dedupe_coll(idx.links, lambda l: l.get("link-uuid") or l.get("link-dbid"))
    _dedupe_coll(idx.vrfs, lambda v: v.get("vrfName"))
    _dedupe_coll(idx.networks, lambda n: n.get("networkName"))
