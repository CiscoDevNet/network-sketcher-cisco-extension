# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""fetch_from_nd.py — pull an NDFC / Fabric Controller model over the REST API.

Companion to ``convert.py``. The converter reads local files, but in a lab it is
convenient to grab the model straight from a reachable Nexus Dashboard via its
REST API and feed the result directly into the converter.

It logs in (Nexus Dashboard ``POST /login`` -> JWT), then for every fabric (or
the ones named with ``--fabric``) pulls the switch inventory, the observed
links, and the VRF / Network overlay, and writes a single *combined JSON* that
``convert.py`` consumes::

    python -m nd_converter.src.fetch_from_nd \\
        --host nd.example.com --user admin \\
        --out nd_converter/Input_data/nd_export.json
    # password via --password or the ND_PASSWORD env var

    python -m nd_converter.src.convert \\
        -i nd_converter/Input_data/nd_export.json -m both \\
        -o nd_converter/Output_data/ns_commands.txt

Credentials come from a CLI arg or the ``ND_PASSWORD`` environment variable;
nothing is hard-coded. Nexus Dashboard uses a self-signed certificate, so TLS
verification is disabled by default (``--verify-tls`` to enforce it).

Retrieval is read-only — this tool never modifies the fabric. Standard library
only (urllib + ssl), no third-party dependencies.
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional


_NDFC = "/appcenter/cisco/ndfc/api/v1"
_LAN = f"{_NDFC}/lan-fabric/rest"


class NdClient:
    """Minimal read-only Nexus Dashboard / NDFC REST client (JWT bearer auth)."""

    def __init__(self, host: str, verify_tls: bool = False, timeout: int = 60,
                 domain: str = "local") -> None:
        self.base = host if host.startswith("http") else f"https://{host}"
        self.timeout = timeout
        self.domain = domain
        self.token: Optional[str] = None
        if verify_tls:
            self.ctx: Optional[ssl.SSLContext] = None
        else:
            self.ctx = ssl.create_default_context()
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE

    def _request(self, path: str, data: Optional[dict] = None) -> Any:
        url = self.base + path
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
            headers["Cookie"] = f"AuthCookie={self.token}"
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(
            url, data=body, headers=headers,
            method="POST" if data is not None else "GET",
        )
        with urllib.request.urlopen(req, context=self.ctx, timeout=self.timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw.strip() else None

    def login(self, user: str, password: str) -> None:
        resp = self._request(
            "/login",
            {"userName": user, "userPasswd": password, "domain": self.domain},
        )
        self.token = resp.get("jwttoken") or resp.get("token")
        if not self.token:
            raise RuntimeError("login succeeded but no JWT token in response")

    def get(self, path: str) -> Any:
        return self._request(path)


# Candidate Endpoint-Locator read paths (vary by NDFC release / EPL enablement).
# The first that returns a non-empty list wins; all 404/405 -> no endpoints.
def _epl_paths(name: str) -> List[str]:
    return [
        f"{_LAN}/control/fabrics/{name}/endpoints",
        f"/appcenter/cisco/ndfc/api/v1/endpointlocator/rest/v1/endpoints?fabric={name}",
        f"{_LAN}/control/fabrics/{name}/eplhistory",
    ]


def fetch_model(client: NdClient, only_fabrics: Optional[List[str]] = None,
                with_endpoints: bool = True, with_interfaces: bool = False,
                with_vpc: bool = True) -> Dict[str, Any]:
    """Return a combined-JSON document ready for ``convert.py``.

    Always pulls the core model (fabrics / switches / links / VRFs / Networks +
    attachments). Optionally also pulls richer sources — vPC pairs, Multi-Site
    associations (cheap, on by default), Endpoint Locator hosts, and per-switch
    interface detail (heavy, off by default). A failed query is logged and
    skipped (never aborts the run), so a partial fetch still works.
    """
    out: Dict[str, Any] = {
        "_meta": {"source": "Cisco Nexus Dashboard / NDFC (Fabric Controller)",
                  "host": client.base},
        "fabrics": [], "switches": {}, "links": {}, "vrfs": {}, "networks": {},
        "vrfAttachments": {}, "networkAttachments": {},
        "endpoints": {}, "vpcPairs": {}, "interfaces": {}, "msdAssociations": {},
    }

    def _grab(path: str, label: str) -> Any:
        try:
            return client.get(path)
        except (urllib.error.URLError, RuntimeError, ValueError) as exc:
            print(f"    {label}: [skipped: {exc}]", file=sys.stderr)
            return None

    fabrics = _grab(f"{_LAN}/control/fabrics", "fabrics") or []
    if not isinstance(fabrics, list):
        fabrics = []
    if only_fabrics:
        fabrics = [f for f in fabrics if f.get("fabricName") in set(only_fabrics)]
    out["fabrics"] = fabrics
    names = [f.get("fabricName") for f in fabrics if f.get("fabricName")]
    print(f"  fabrics: {names}", file=sys.stderr)

    # Global (cross-fabric) sources, fetched once and filtered per fabric.
    vpc_global = (_grab(f"{_LAN}/vpcpair", "vpc pairs") if with_vpc else None) or []
    msd_global = (_grab(f"{_LAN}/control/fabrics/msd/fabric-associations",
                        "MSD associations") if with_vpc else None) or []

    def _for_fabric(items: List[dict], name: str) -> List[dict]:
        if not isinstance(items, list):
            return []
        tagged = [i for i in items if i.get("fabricName") == name or i.get("fabric") == name]
        return tagged or items  # if entries carry no fabric tag, keep them all

    for name in names:
        print(f"  fabric '{name}':", file=sys.stderr)
        sw = _grab(f"{_LAN}/control/fabrics/{name}/inventory/switchesByFabric", "switches") or []
        lk = _grab(f"{_LAN}/control/links/fabrics/{name}", "links") or []
        vrf = _grab(f"{_LAN}/top-down/fabrics/{name}/vrfs", "vrfs") or []
        net = _grab(f"{_LAN}/top-down/fabrics/{name}/networks", "networks") or []
        vatt = _grab(f"{_LAN}/top-down/fabrics/{name}/vrfs/attachments", "vrf attachments") or []
        natt = _grab(f"{_LAN}/top-down/fabrics/{name}/networks/attachments", "network attachments") or []
        out["switches"][name] = sw if isinstance(sw, list) else []
        out["links"][name] = lk if isinstance(lk, list) else []
        out["vrfs"][name] = vrf if isinstance(vrf, list) else []
        out["networks"][name] = net if isinstance(net, list) else []
        out["vrfAttachments"][name] = vatt if isinstance(vatt, list) else []
        out["networkAttachments"][name] = natt if isinstance(natt, list) else []
        out["vpcPairs"][name] = _for_fabric(vpc_global, name) if with_vpc else []
        out["msdAssociations"][name] = _for_fabric(msd_global, name) if with_vpc else []

        # Endpoint Locator hosts (optional; path varies / often disabled).
        eps: List[dict] = []
        if with_endpoints:
            for p in _epl_paths(name):
                r = _grab(p, "endpoints")
                if isinstance(r, list) and r:
                    eps = r
                    break
        out["endpoints"][name] = eps

        # Per-switch interface detail (optional, heavy). Tag each with its serial.
        itfs: List[dict] = []
        if with_interfaces:
            for s in out["switches"][name]:
                ser = s.get("serialNumber")
                if not ser:
                    continue
                r = _grab(f"{_LAN}/interface/detail?serialNumber={ser}", f"interfaces {ser}")
                for itf in (r or []):
                    if isinstance(itf, dict):
                        itf.setdefault("serialNumber", ser)
                        itf.setdefault("switchName", s.get("logicalName"))
                        itfs.append(itf)
        out["interfaces"][name] = itfs

        print(f"    switches={len(out['switches'][name])} links={len(out['links'][name])} "
              f"vrfs={len(out['vrfs'][name])} networks={len(out['networks'][name])} "
              f"endpoints={len(eps)} vpc={len(out['vpcPairs'][name])} interfaces={len(itfs)}",
              file=sys.stderr)

    return out


def main(argv: Optional[list] = None) -> int:
    p = argparse.ArgumentParser(
        description="Pull an NDFC / Fabric Controller model over the Nexus Dashboard REST API (read-only).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--host", required=True, help="Nexus Dashboard IP / hostname.")
    p.add_argument("--user", default="admin", help="ND username (default: admin).")
    p.add_argument("--password", default=None,
                   help="ND password (or set the ND_PASSWORD env var).")
    p.add_argument("--domain", default="local",
                   help="ND login domain / auth realm (default: local).")
    p.add_argument("--fabric", action="append", default=None,
                   help="Limit to this fabric (repeatable). Default: all fabrics.")
    p.add_argument("--verify-tls", action="store_true",
                   help="Enforce TLS cert verification (ND uses self-signed certs by default).")
    p.add_argument("--no-endpoints", action="store_true",
                   help="Skip Endpoint Locator host discovery (on by default; harmless if EPL is off).")
    p.add_argument("--no-vpc", action="store_true",
                   help="Skip vPC-pair and Multi-Site association discovery (on by default; cheap).")
    p.add_argument("--with-interfaces", action="store_true",
                   help="Also pull per-switch interface detail (heavy: one call per switch). "
                        "Gives real port speed/duplex/media in the underlay.")
    p.add_argument("--out", default="nd_export.json",
                   help="Output JSON path (default: nd_export.json).")
    args = p.parse_args(argv)

    password = args.password or os.environ.get("ND_PASSWORD")
    if not password:
        print("[ERROR] ND password required: pass --password or set ND_PASSWORD.",
              file=sys.stderr)
        return 2

    client = NdClient(args.host, verify_tls=args.verify_tls, domain=args.domain)
    try:
        client.login(args.user, password)
    except (urllib.error.URLError, RuntimeError, KeyError, ValueError) as exc:
        print(f"[ERROR] Nexus Dashboard login failed: {exc}", file=sys.stderr)
        return 1
    print(f"[ok] authenticated to {args.host} as {args.user}", file=sys.stderr)

    try:
        doc = fetch_model(client, only_fabrics=args.fabric,
                          with_endpoints=not args.no_endpoints,
                          with_interfaces=args.with_interfaces,
                          with_vpc=not args.no_vpc)
    except (urllib.error.URLError, RuntimeError) as exc:
        print(f"[ERROR] fetch failed: {exc}", file=sys.stderr)
        return 1

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, ensure_ascii=False)
    nfab = len(doc.get("fabrics", []))
    print(f"[ok] wrote {args.out} ({nfab} fabric(s))", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
