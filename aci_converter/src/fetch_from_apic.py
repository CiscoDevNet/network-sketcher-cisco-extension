# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""fetch_from_apic.py — pull an APIC policy model over the REST API.

Companion to ``convert.py``. The converter reads local files, but in a lab it
is convenient to grab the policy model straight from a reachable APIC via its
REST API and feed the result directly into the converter.

It logs in, pulls ``fabricNodeIdentP`` (fabric membership) plus every
``fvTenant`` subtree (``rsp-subtree=full&rsp-prop-include=config-only``), and
writes a single ``{"imdata":[...]}`` JSON that ``convert.py`` consumes exactly
like a downloaded Configuration Export::

    python -m aci_converter.src.fetch_from_apic \\
        --host apic.example.com --user admin \\
        --out aci_converter/Input_data/apic_export.json
    # password via --password or the ACI_PASSWORD env var

    python -m aci_converter.src.convert \\
        -i aci_converter/Input_data/apic_export.json -m both \\
        -o aci_converter/Output_data/ns_commands.txt

Credentials come from a CLI arg or the ``ACI_PASSWORD`` environment variable;
nothing is hard-coded. The APIC uses a self-signed certificate, so TLS
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


class ApicClient:
    """Minimal read-only APIC REST client (token cookie auth)."""

    def __init__(self, host: str, verify_tls: bool = False, timeout: int = 30) -> None:
        self.base = host if host.startswith("http") else f"https://{host}"
        self.timeout = timeout
        self.token: Optional[str] = None
        if verify_tls:
            self.ctx: Optional[ssl.SSLContext] = None
        else:
            self.ctx = ssl.create_default_context()
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE

    def _request(self, path: str, data: Optional[dict] = None) -> dict:
        url = self.base + path
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Cookie"] = f"APIC-cookie={self.token}"
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(
            url, data=body, headers=headers,
            method="POST" if data is not None else "GET",
        )
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            # Surface the APIC's error text (imdata[0].error.attributes.text).
            detail = ""
            try:
                err_body = exc.read().decode("utf-8", errors="replace")
                parsed = json.loads(err_body)
                items = parsed.get("imdata", [])
                if items:
                    attrs = next(iter(items[0].values())).get("attributes", {})
                    detail = attrs.get("text") or err_body
                else:
                    detail = err_body
            except (ValueError, AttributeError, StopIteration):
                detail = detail or "(no error body)"
            raise RuntimeError(f"HTTP {exc.code} from {path}: {detail}") from exc

    def login(self, user: str, password: str) -> None:
        resp = self._request(
            "/api/aaaLogin.json",
            {"aaaUser": {"attributes": {"name": user, "pwd": password}}},
        )
        self.token = resp["imdata"][0]["aaaLogin"]["attributes"]["token"]

    def get_class(self, cls: str, query: str = "") -> List[dict]:
        path = f"/api/class/{cls}.json"
        if query:
            path += f"?{query}"
        return self._request(path).get("imdata", [])


# Operational / access-policy classes fetched by default (disable with
# --no-topology) so the diagrams reflect the real fabric:
#   fabricNode      authoritative role + model + APIC controllers
#   topSystem       per-node mgmt/TEP IP, version, pod
#   lldpAdjEp       observed spine/leaf/APIC cabling
#   fvCEp/fvIp/...  real endpoints (MAC/IP) and their leaf/port
#   vpc*/fabric*GEp vPC leaf pairs
#   infra*/fvns*    access policies (AEP / VLAN pools / domains / port selectors)
_TOPOLOGY_CLASSES = [
    "fabricNode", "topSystem", "lldpAdjEp",
    "fvCEp", "fvIp", "fvRsCEpToPathEp",
    "fabricExplicitGEp", "fabricNodePEp", "vpcDom",
    "infraAttEntityP", "fvnsVlanInstP", "physDomP",
    "infraHPortS", "infraRsAccBaseGrp",
]


def fetch_mit(client: ApicClient, with_topology: bool = True) -> Dict[str, Any]:
    """Return a config-export-equivalent {"imdata":[...]} document.

    Always pulls the policy model: ``fabricNodeIdentP`` (fabric membership) and
    every ``fvTenant`` subtree (config-only). With ``with_topology`` (the
    default) it additionally pulls operational + access-policy classes
    (:data:`_TOPOLOGY_CLASSES`) so the underlay uses real roles / APIC / cabling,
    the overlay can show real endpoints, and the unified view can place EPGs and
    endpoints on their actual leaf ports. Pass ``with_topology=False``
    (``--no-topology``) for a pure config-only export.
    """
    imdata: List[dict] = []

    def _grab(cls: str, query: str = "", label: str = "") -> List[dict]:
        """Fetch one class, appending results. A failed query is logged and
        skipped (never aborts the run) so we always keep whatever we did get."""
        try:
            mos = client.get_class(cls, query)
        except (urllib.error.URLError, RuntimeError, ValueError) as exc:
            print(f"  {label or cls}: [skipped: {exc}]", file=sys.stderr)
            return []
        imdata.extend(mos)
        print(f"  {label or cls}: {len(mos)}", file=sys.stderr)
        return mos

    _grab("fabricNodeIdentP")
    tenants = _grab("fvTenant", "rsp-subtree=full&rsp-prop-include=config-only")
    names = [t["fvTenant"]["attributes"].get("name") for t in tenants if "fvTenant" in t]
    if names:
        print(f"    tenants: {names}", file=sys.stderr)

    if with_topology:
        for cls in _TOPOLOGY_CLASSES:
            _grab(cls)

    return {"totalCount": str(len(imdata)), "imdata": imdata}


def main(argv: Optional[list] = None) -> int:
    p = argparse.ArgumentParser(
        description="Pull an APIC policy model over the REST API (read-only).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--host", required=True, help="APIC IP / hostname.")
    p.add_argument("--user", default="admin", help="APIC username (default: admin).")
    p.add_argument("--password", default=None,
                   help="APIC password (or set the ACI_PASSWORD env var).")
    p.add_argument("--verify-tls", action="store_true",
                   help="Enforce TLS cert verification (APICs use self-signed certs by default).")
    p.add_argument("--out", default="apic_export.json",
                   help="Output JSON path (default: apic_export.json).")
    p.add_argument("--no-topology", action="store_true",
                   help="Fetch ONLY the config-only policy model (fabricNodeIdentP + "
                        "fvTenant). By default the tool also pulls operational + "
                        "access-policy classes (fabricNode, topSystem, lldpAdjEp, "
                        "fvCEp/fvIp, vPC, access policies) for richer diagrams.")
    args = p.parse_args(argv)

    password = args.password or os.environ.get("ACI_PASSWORD")
    if not password:
        print("[ERROR] APIC password required: pass --password or set ACI_PASSWORD.",
              file=sys.stderr)
        return 2

    client = ApicClient(args.host, verify_tls=args.verify_tls)
    try:
        client.login(args.user, password)
    except (urllib.error.URLError, RuntimeError, KeyError, ValueError) as exc:
        print(f"[ERROR] APIC login failed: {exc}", file=sys.stderr)
        return 1
    print(f"[ok] authenticated to {args.host} as {args.user}", file=sys.stderr)

    try:
        doc = fetch_mit(client, with_topology=not args.no_topology)
    except (urllib.error.URLError, RuntimeError) as exc:
        print(f"[ERROR] fetch failed: {exc}", file=sys.stderr)
        return 1

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)
    print(f"[ok] wrote {args.out} ({len(doc['imdata'])} top-level MOs)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
