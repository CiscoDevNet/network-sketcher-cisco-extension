# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""Ingest an APIC Configuration Export and index its Managed Objects.

An APIC config export is the policy half of the Management Information Tree
(MIT), rooted at ``polUni``. It ships in one of three shapes that this reader
all collapses into a single :class:`AciIndex`:

  * a ``.tar.gz`` / ``.tgz`` backup containing one or more ``*.json`` members,
  * a directory of ``*.json`` files (an extracted backup), or
  * a single merged ``*.json`` file.

Every Managed Object (MO) in the JSON is the one-key dict shape::

    {"fvTenant": {"attributes": {"name": "...", "dn": "..."}, "children": [ ... ]}}

The export wraps the tree either as ``{"imdata": [ <mo>, ... ]}`` (the REST
query envelope) or as a bare top-level MO (``{"polUni": {...}}``). Children
usually carry only ``rn`` (relative name), so DNs are synthesised from the
parent DN + ``rn`` to keep DN-based joins (``fvRsBd.tDn`` → BD,
``fvRsPathAtt.tDn`` → leaf/port) resolvable.

The export contains the *policy / logical* model and node registration, but
NOT operational state (no ``lldpAdjEp`` / ``topSystem`` / ``fvCEp``); the
physical mapper therefore infers fabric cabling.
"""
from __future__ import annotations

import json
import pathlib
import sys
import tarfile
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional


@dataclass
class Mo:
    """A single Managed Object node from the MIT."""
    cls: str
    dn: str
    attrs: Dict[str, Any]
    parent_dn: Optional[str] = None
    children_dns: List[str] = field(default_factory=list)

    def get(self, key: str, default: str = "") -> str:
        val = self.attrs.get(key, default)
        return "" if val is None else str(val)


class AciIndex:
    """Index of every MO, keyed by className and by DN."""

    def __init__(self) -> None:
        self.by_class: Dict[str, List[Mo]] = defaultdict(list)
        self.by_dn: Dict[str, Mo] = {}
        self._doc_count = 0

    def add(self, cls: str, dn: str, attrs: Dict[str, Any], parent_dn: Optional[str]) -> Mo:
        existing = self.by_dn.get(dn) if dn else None
        if existing is not None:
            # Same DN seen across split files: merge attributes (later wins for
            # populated values) rather than duplicating the node.
            for k, v in attrs.items():
                if v not in (None, ""):
                    existing.attrs[k] = v
            return existing
        mo = Mo(cls=cls, dn=dn, attrs=dict(attrs), parent_dn=parent_dn)
        self.by_class[cls].append(mo)
        if dn:
            self.by_dn[dn] = mo
        if parent_dn and parent_dn in self.by_dn:
            self.by_dn[parent_dn].children_dns.append(dn)
        return mo

    def of(self, cls: str) -> List[Mo]:
        """All MOs of a className (empty list if none)."""
        return self.by_class.get(cls, [])

    def get(self, dn: str) -> Optional[Mo]:
        return self.by_dn.get(dn)

    def children_of(self, mo: Mo, cls: Optional[str] = None) -> List[Mo]:
        """Resolved child MOs of ``mo``, optionally filtered by className."""
        out: List[Mo] = []
        for dn in mo.children_dns:
            child = self.by_dn.get(dn)
            if child is None:
                continue
            if cls is None or child.cls == cls:
                out.append(child)
        return out

    def summary(self) -> Dict[str, int]:
        """Per-class object counts (for the audit report)."""
        return {cls: len(mos) for cls, mos in sorted(self.by_class.items())}


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_mit(path: pathlib.Path) -> AciIndex:
    """Load an APIC config export from a tar.gz, a directory, or a single JSON."""
    docs = _read_json_documents(path)
    idx = AciIndex()
    for doc in docs:
        idx._doc_count += 1
        for root_mo in _iter_top_level_mos(doc):
            _walk(root_mo, idx, parent_dn=None)
    return idx


def _read_json_documents(path: pathlib.Path) -> List[Any]:
    """Return a list of parsed JSON documents from any supported input shape."""
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
                docs.append(doc)
        return docs

    # A file: tar.gz / tgz, or a single JSON.
    if tarfile.is_tarfile(path):
        with tarfile.open(path, "r:*") as tar:
            members = [m for m in tar.getmembers() if m.isfile()]
            json_members = [m for m in members if m.name.lower().endswith(".json")]
            if not json_members:
                raise ValueError(
                    f"archive {path} contains no *.json members "
                    f"(found {len(members)} file(s)). XML exports are not supported; "
                    "re-run the APIC export with JSON format."
                )
            for m in json_members:
                fh = tar.extractfile(m)
                if fh is None:
                    continue
                try:
                    docs.append(json.loads(fh.read().decode("utf-8", errors="replace")))
                except json.JSONDecodeError as exc:
                    print(f"[WARN] skipping unparseable JSON member {m.name}: {exc}",
                          file=sys.stderr)
        return docs

    if path.suffix.lower() == ".xml":
        raise ValueError(
            "XML exports are not supported. Re-run the APIC Configuration Export "
            "with the JSON format (or extract the .tar.gz containing JSON)."
        )

    doc = _load_json_file(path)
    if doc is None:
        raise ValueError(f"could not parse JSON from {path}")
    docs.append(doc)
    return docs


def _load_json_file(p: pathlib.Path) -> Any:
    try:
        with p.open(encoding="utf-8-sig", errors="replace") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[WARN] skipping unparseable JSON file {p}: {exc}", file=sys.stderr)
        return None


def _iter_top_level_mos(doc: Any) -> Iterable[Dict[str, Any]]:
    """Yield the top-level MO dicts from a parsed document.

    Handles the ``{"imdata": [...]}`` REST envelope, a bare MO dict
    (``{"polUni": {...}}``), and a bare list of MOs.
    """
    if isinstance(doc, dict):
        if "imdata" in doc and isinstance(doc["imdata"], list):
            for mo in doc["imdata"]:
                if isinstance(mo, dict) and mo:
                    yield mo
            return
        # A bare MO dict: one className key mapping to {attributes, children}.
        if len(doc) >= 1 and all(isinstance(v, dict) for v in doc.values()):
            yield doc
            return
    if isinstance(doc, list):
        for mo in doc:
            if isinstance(mo, dict) and mo:
                yield mo


# ACI relative-name (rn) reconstruction, used when a config-only subtree omits
# both dn and rn from children. Maps className -> how to build its rn.
_RN_SINGLETON = {  # rn is a fixed token (one instance under the parent)
    "fvRsBd": "rsbd", "fvRsCtx": "rsctx", "l3extRsEctx": "rsectx",
}
_RN_NAME = {  # rn = "<prefix><name>"
    "fvTenant": "tn-", "fvCtx": "ctx-", "fvBD": "BD-", "fvAp": "ap-",
    "fvAEPg": "epg-", "vzBrCP": "brc-", "vzSubj": "subj-", "vzFilter": "flt-",
    "vzEntry": "e-", "l3extOut": "out-", "l3extInstP": "instP-",
    "l3extLNodeP": "lnodep-",
}
_RN_REF = {  # rn = "<prefix><attr-value>" (relation target name, no brackets)
    "fvRsProv": ("rsprov-", "tnVzBrCPName"),
    "fvRsCons": ("rscons-", "tnVzBrCPName"),
    "vzRsSubjFiltAtt": ("rssubjFiltAtt-", "tnVzFilterName"),
}
_RN_BRACKET = {  # rn = "<prefix>[<attr-value>]"
    "fvSubnet": ("subnet-", "ip"), "l3extSubnet": ("extsubnet-", "ip"),
    "fvRsPathAtt": ("rspathAtt-", "tDn"),
    "fvRsDomAtt": ("rsdomAtt-", "tDn"),
    "l3extRsNodeL3OutAtt": ("rsnodeL3OutAtt-", "tDn"),
}


def _synth_rn(cls: str, attrs: Dict[str, Any]) -> str:
    """Rebuild an MO's ACI relative name from its class + naming attribute."""
    if cls in _RN_SINGLETON:
        return _RN_SINGLETON[cls]
    if cls in _RN_NAME:
        v = attrs.get("name")
        return f"{_RN_NAME[cls]}{v}" if v else ""
    if cls in _RN_REF:
        pre, key = _RN_REF[cls]
        v = attrs.get(key)
        return f"{pre}{v}" if v else ""
    if cls in _RN_BRACKET:
        pre, key = _RN_BRACKET[cls]
        v = attrs.get(key)
        return f"{pre}[{v}]" if v else ""
    return ""


def _walk(mo: Dict[str, Any], idx: AciIndex, parent_dn: Optional[str]) -> None:
    """Recursively index one MO and its children."""
    if not isinstance(mo, dict) or not mo:
        return
    # An MO is a single-key dict; tolerate stray multi-key dicts by taking each.
    for cls, body in mo.items():
        if not isinstance(body, dict):
            continue
        attrs = body.get("attributes") or {}
        if not isinstance(attrs, dict):
            attrs = {}
        dn = str(attrs.get("dn") or "").strip()
        if not dn:
            # A ``rsp-prop-include=config-only`` subtree strips both dn AND rn
            # from children, leaving only config attrs. Reconstruct the ACI
            # relative name from the class + its naming attribute so DN-based
            # joins (EPG <-> endpoints, contracts, BDs) resolve to the REAL dn.
            rn = str(attrs.get("rn") or "").strip() or _synth_rn(cls, attrs)
            if parent_dn and rn:
                dn = f"{parent_dn}/{rn}"
            elif rn:
                dn = rn
            else:
                # No identity at all: synthesise a positional placeholder so the
                # node is still indexed by class (joins won't target it).
                dn = f"{parent_dn or ''}/{cls}-{len(idx.by_class.get(cls, []))}"
        node = idx.add(cls, dn, attrs, parent_dn)
        children = body.get("children") or []
        if isinstance(children, list):
            for child in children:
                _walk(child, idx, node.dn)
