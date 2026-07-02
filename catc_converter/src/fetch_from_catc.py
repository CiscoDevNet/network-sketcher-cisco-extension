# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""fetch_from_catc.py — pull a Cisco Catalyst Center (DNA Center) model over the REST API.

Companion to ``convert.py``. The converter reads local files, but in a lab it is
convenient to grab the model straight from a reachable Catalyst Center via its
Intent REST API and feed the result directly into the converter.

It authenticates (``POST /dna/system/api/v1/auth/token`` with HTTP Basic auth ->
``{"Token": "<jwt>"}``), then pulls the network-device inventory, the physical
topology, the sites, and the SD-Access overlay (fabric sites / fabric devices /
Layer3 VNs / anycast gateways), and writes a single *combined JSON* that
``convert.py`` consumes::

    python -m catc_converter.src.fetch_from_catc \\
        --host catc.example.com --user admin \\
        --out catc_converter/Input_data/catc_export.json
    # password via --password or the CATC_PASSWORD env var

    python -m catc_converter.src.convert \\
        -i catc_converter/Input_data/catc_export.json -m both \\
        -o catc_converter/Output_data/ns_commands.txt

Credentials come from a CLI arg or the ``CATC_PASSWORD`` environment variable;
nothing is hard-coded. Catalyst Center uses a self-signed certificate, so TLS
verification is disabled by default (``--verify-tls`` to enforce it).

Retrieval is read-only — this tool never modifies the fabric. Standard library
only (urllib + ssl), no third-party dependencies.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional


_PAGE_LIMIT = 500


class CatcClient:
    """Minimal read-only Catalyst Center / DNA Center REST client (X-Auth-Token)."""

    def __init__(self, host: str, verify_tls: bool = False, timeout: int = 60) -> None:
        self.base = host if host.startswith("http") else f"https://{host}"
        self.timeout = timeout
        self.token: Optional[str] = None
        if verify_tls:
            self.ctx: Optional[ssl.SSLContext] = None
        else:
            self.ctx = ssl.create_default_context()
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE

    def _open(self, req: urllib.request.Request) -> Any:
        with urllib.request.urlopen(req, context=self.ctx, timeout=self.timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw.strip() else None

    def login(self, user: str, password: str) -> None:
        """POST /dna/system/api/v1/auth/token with HTTP Basic auth, empty body."""
        creds = base64.b64encode(f"{user}:{password}".encode()).decode()
        req = urllib.request.Request(
            self.base + "/dna/system/api/v1/auth/token",
            data=b"",
            headers={"Authorization": f"Basic {creds}",
                     "Content-Type": "application/json"},
            method="POST",
        )
        resp = self._open(req)
        self.token = (resp or {}).get("Token")
        if not self.token:
            raise RuntimeError("login succeeded but no Token in response")

    def get(self, path: str) -> Any:
        """GET <path> with the X-Auth-Token header."""
        req = urllib.request.Request(
            self.base + path,
            headers={"X-Auth-Token": self.token or "",
                     "Content-Type": "application/json"},
            method="GET",
        )
        return self._open(req)


def _response(block: Any) -> Any:
    """Unwrap the Catalyst Center ``{"response": ...}`` envelope."""
    if isinstance(block, dict) and "response" in block:
        return block["response"]
    return block


def _paged(client: "CatcClient", base_path: str, grab) -> List[dict]:
    """Page a collection via ?offset=N&limit=500 (offset is 1-based).

    Loops until a page returns fewer than ``limit`` items.
    """
    out: List[dict] = []
    offset = 1
    sep = "&" if "?" in base_path else "?"
    while True:
        page = _response(grab(f"{base_path}{sep}offset={offset}&limit={_PAGE_LIMIT}",
                              f"{base_path} (offset {offset})"))
        items = page if isinstance(page, list) else []
        out.extend(i for i in items if isinstance(i, dict))
        if len(items) < _PAGE_LIMIT:
            break
        offset += _PAGE_LIMIT
    return out


def fetch_model(client: CatcClient, with_endpoints: bool = False,
                with_interfaces: bool = False) -> Dict[str, Any]:
    """Return a combined-JSON document ready for ``convert.py``.

    Always pulls the core model (devices / physical topology / sites + the
    SD-Access overlay: fabric sites, fabric devices, Layer3 VNs, anycast
    gateways). Optionally also pulls per-device interface detail (heavy) and the
    clients collection. A failed query is logged and skipped (never aborts the
    run), so a partial fetch still works.
    """
    out: Dict[str, Any] = {
        "_meta": {"source": "Cisco Catalyst Center / DNA Center", "host": client.base},
        "devices": [], "interfaces": {}, "physicalTopology": {},
        "sites": [], "fabricSites": [], "fabricDevices": [],
        "layer3VirtualNetworks": [], "anycastGateways": [], "clients": [],
        "ssids": [], "wirelessProfiles": [],
    }

    def _grab(path: str, label: str) -> Any:
        try:
            return client.get(path)
        except (urllib.error.URLError, RuntimeError, ValueError) as exc:
            print(f"    {label}: [skipped: {exc}]", file=sys.stderr)
            return None

    # Network device inventory (paged).
    out["devices"] = _paged(client, "/dna/intent/api/v1/network-device", _grab)
    print(f"  devices: {len(out['devices'])}", file=sys.stderr)

    # Physical topology (nodes + links).
    pt = _response(_grab("/dna/intent/api/v1/topology/physical-topology", "physical topology"))
    if isinstance(pt, dict):
        out["physicalTopology"] = {"nodes": pt.get("nodes") or [], "links": pt.get("links") or []}
    print(f"  topology: nodes={len(out['physicalTopology'].get('nodes', []))} "
          f"links={len(out['physicalTopology'].get('links', []))}", file=sys.stderr)

    # Sites.
    sites = _response(_grab("/dna/intent/api/v1/site", "sites"))
    out["sites"] = sites if isinstance(sites, list) else []

    # SD-Access fabric sites (the join point for fabricDevices / anycastGateways).
    fsites = _response(_grab("/dna/intent/api/v1/sda/fabricSites", "fabric sites"))
    out["fabricSites"] = fsites if isinstance(fsites, list) else []
    fabric_ids = [str(f.get("id")) for f in out["fabricSites"] if f.get("id")]
    print(f"  sites={len(out['sites'])} fabricSites={len(out['fabricSites'])}", file=sys.stderr)

    # fabricDevices + anycastGateways REQUIRE a fabricId query param; loop ids.
    for fid in fabric_ids:
        fd = _response(_grab(f"/dna/intent/api/v1/sda/fabricDevices?fabricId={fid}",
                             f"fabric devices (fabric {fid})"))
        if isinstance(fd, list):
            out["fabricDevices"].extend(d for d in fd if isinstance(d, dict))
        ag = _response(_grab(f"/dna/intent/api/v1/sda/anycastGateways?fabricId={fid}",
                             f"anycast gateways (fabric {fid})"))
        if isinstance(ag, list):
            out["anycastGateways"].extend(g for g in ag if isinstance(g, dict))

    # Layer3 Virtual Networks (cross-fabric).
    l3 = _response(_grab("/dna/intent/api/v1/sda/layer3VirtualNetworks", "L3 virtual networks"))
    out["layer3VirtualNetworks"] = l3 if isinstance(l3, list) else []
    print(f"  fabricDevices={len(out['fabricDevices'])} "
          f"layer3VNs={len(out['layer3VirtualNetworks'])} "
          f"anycastGateways={len(out['anycastGateways'])}", file=sys.stderr)

    # Wireless SSIDs + profiles (global, cheap) — used to model FlexConnect APs'
    # client VLANs and to draw a dummy wireless client per SSID (RULE 12 / the
    # meraki_converter dummy-wireless-client convention).
    ssids = _response(_grab("/dna/intent/api/v1/wirelessSettings/ssids", "wireless SSIDs"))
    out["ssids"] = ssids if isinstance(ssids, list) else []
    profiles = _response(_grab("/dna/intent/api/v1/wireless/profile", "wireless profiles"))
    out["wirelessProfiles"] = profiles if isinstance(profiles, list) else []
    print(f"  ssids={len(out['ssids'])} wirelessProfiles={len(out['wirelessProfiles'])}",
          file=sys.stderr)

    # Clients (optional; may be empty).
    if with_endpoints:
        clients = _response(_grab("/dna/data/api/v1/clients?limit=500", "clients"))
        out["clients"] = clients if isinstance(clients, list) else []
        print(f"  clients={len(out['clients'])}", file=sys.stderr)

    # Per-device interface detail (optional, heavy: one call per device).
    if with_interfaces:
        for dev in out["devices"]:
            did = dev.get("id")
            if not did:
                continue
            r = _response(_grab(f"/dna/intent/api/v1/interface/network-device/{did}",
                                f"interfaces {did}"))
            out["interfaces"][str(did)] = [i for i in (r or []) if isinstance(i, dict)]
        total = sum(len(v) for v in out["interfaces"].values())
        print(f"  interfaces={total} (across {len(out['interfaces'])} devices)", file=sys.stderr)

    return out


def main(argv: Optional[list] = None) -> int:
    p = argparse.ArgumentParser(
        description="Pull a Cisco Catalyst Center / DNA Center model over the Intent REST API (read-only).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--host", required=True, help="Catalyst Center IP / hostname.")
    p.add_argument("--user", default="admin", help="Catalyst Center username (default: admin).")
    p.add_argument("--password", default=None,
                   help="Catalyst Center password (or set the CATC_PASSWORD env var).")
    p.add_argument("--verify-tls", action="store_true",
                   help="Enforce TLS cert verification (Catalyst Center uses self-signed certs by default).")
    p.add_argument("--with-endpoints", action="store_true",
                   help="Also pull the clients collection (for overlay host devices; may be empty).")
    p.add_argument("--with-interfaces", action="store_true",
                   help="Also pull per-device interface detail (heavy: one call per device). "
                        "Gives real port speed/duplex/media in the underlay.")
    p.add_argument("--out", default="catc_export.json",
                   help="Output JSON path (default: catc_export.json).")
    args = p.parse_args(argv)

    password = args.password or os.environ.get("CATC_PASSWORD")
    if not password:
        print("[ERROR] Catalyst Center password required: pass --password or set CATC_PASSWORD.",
              file=sys.stderr)
        return 2

    client = CatcClient(args.host, verify_tls=args.verify_tls)
    try:
        client.login(args.user, password)
    except (urllib.error.URLError, RuntimeError, KeyError, ValueError) as exc:
        print(f"[ERROR] Catalyst Center login failed: {exc}", file=sys.stderr)
        return 1
    print(f"[ok] authenticated to {args.host} as {args.user}", file=sys.stderr)

    try:
        doc = fetch_model(client, with_endpoints=args.with_endpoints,
                          with_interfaces=args.with_interfaces)
    except (urllib.error.URLError, RuntimeError) as exc:
        print(f"[ERROR] fetch failed: {exc}", file=sys.stderr)
        return 1

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, ensure_ascii=False)
    ndev = len(doc.get("devices", []))
    print(f"[ok] wrote {args.out} ({ndev} device(s))", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
