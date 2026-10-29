# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""fetch_from_cml.py — pull CML lab topologies over the read-only REST API.

Companion to ``convert.py``. The converter reads local files, but in a lab it
is convenient to grab every lab straight from a reachable CML controller and
feed the result into the converter. An administrator account also receives
labs owned by other users (``GET /api/v0/labs?show_all=true``). Each lab
becomes one Network Sketcher area when that JSON is converted.

    python -m cml_converter.src.fetch_from_cml \\
        --host cml.example.com --user administrator \\
        --out cml_converter/Input_data/cml_export.json
    # password via --password or the CML_PASSWORD env var

    python -m cml_converter.src.convert \\
        --yaml cml_converter/Input_data/cml_export.json \\
        --out cml_converter/Output_data/ns_commands.txt

Credentials come from a CLI argument or the ``CML_PASSWORD`` environment
variable; nothing is hard-coded and the password is never logged. CML
controllers use a self-signed certificate, so TLS verification is disabled
by default (``--verify-tls`` to enforce it).

Retrieval is read-only — this tool never starts, stops, or edits a lab.
Standard library only (``urllib`` + ``ssl``).
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

# Without show_all, CML returns only labs owned by the logged-in user.
_LABS_PATH = "/api/v0/labs?show_all=true"


class CmlClient:
    """Minimal read-only CML REST client (bearer token)."""

    def __init__(self, host: str, verify_tls: bool = False, timeout: int = 60) -> None:
        base = host if host.startswith("http") else f"https://{host}"
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.token: Optional[str] = None
        if verify_tls:
            self.ctx: Optional[ssl.SSLContext] = None
        else:
            self.ctx = ssl.create_default_context()
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE

    def _request(self, path: str, data: Optional[dict] = None) -> Any:
        url = self.base + path
        headers = {"Accept": "application/json"}
        body = None
        if data is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(data).encode()
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(
            url, data=body, headers=headers,
            method="POST" if data is not None else "GET",
        )
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise RuntimeError(f"HTTP {exc.code} {path}: {detail}") from exc
        if not raw:
            return None
        return json.loads(raw)

    def login(self, user: str, password: str) -> None:
        """Authenticate. The password is not stored or logged."""
        token = self._request(
            "/api/v0/authenticate",
            {"username": user, "password": password},
        )
        if isinstance(token, dict):
            token = token.get("token") or token.get("access_token") or ""
        if not isinstance(token, str) or not token.strip():
            raise RuntimeError("authenticate returned an unexpected response")
        self.token = token.strip()


def probe_access(client: CmlClient) -> None:
    """Confirm that the authenticated account can list labs."""
    labs = client._request(_LABS_PATH)
    if not isinstance(labs, list):
        raise RuntimeError("labs API returned an unexpected response")


def _lab_id(item: Any) -> Optional[str]:
    if isinstance(item, str) and item.strip():
        return item.strip()
    if isinstance(item, dict):
        lid = item.get("id") or item.get("lab_id")
        if isinstance(lid, str) and lid.strip():
            return lid.strip()
    return None


def choose_labs(catalog: List[tuple], wanted: Optional[List[str]]) -> List[tuple]:
    """Filter ``(lab_id, lab_title)`` pairs by id or exact title.

    ``wanted`` empty or None keeps every lab, in catalog order.
    """
    if not wanted:
        return list(catalog)
    chosen: List[tuple] = []
    seen = set()
    for needle in wanted:
        matches = [row for row in catalog if needle in (row[0], row[1])]
        if not matches:
            known = ", ".join(f"{title} ({lid})" for lid, title in catalog) or "(none)"
            raise RuntimeError(f"lab not found: {needle}. Known labs: {known}")
        for row in matches:
            if row[0] not in seen:
                chosen.append(row)
                seen.add(row[0])
    return chosen


def strip_credentials(nodes: List[Any]) -> int:
    """Remove each node's pyATS block (username / password / enable_password).

    The converter never reads it, and it must not reach the export file.
    Returns how many nodes had one.
    """
    removed = 0
    for node in nodes:
        if isinstance(node, dict) and "pyats" in node:
            del node["pyats"]
            removed += 1
    return removed


def fetch_labs(client: CmlClient, wanted: Optional[List[str]] = None) -> Dict[str, Any]:
    """Download every selected lab's topology. Read-only."""
    listing = client._request(_LABS_PATH)
    if not isinstance(listing, list):
        raise RuntimeError("labs API returned an unexpected response")
    catalog: List[tuple] = []
    for item in listing:
        lid = _lab_id(item)
        if not lid:
            continue
        meta = client._request(f"/api/v0/labs/{lid}")
        if not isinstance(meta, dict):
            raise RuntimeError(f"lab {lid} returned an unexpected response")
        title = str(meta.get("lab_title") or lid)
        catalog.append((lid, title, meta))
    selected = choose_labs([(lid, title) for lid, title, _meta in catalog], wanted)
    meta_by_id = {lid: meta for lid, _title, meta in catalog}
    labs_out: List[Dict[str, Any]] = []
    for lid, title in selected:
        topo = client._request(f"/api/v0/labs/{lid}/topology")
        if not isinstance(topo, dict):
            raise RuntimeError(f"topology for {title} returned an unexpected response")
        nodes = topo.get("nodes") or []
        links = topo.get("links") or []
        if not isinstance(nodes, list) or not isinstance(links, list):
            raise RuntimeError(f"topology for {title} has no node/link lists")
        strip_credentials(nodes)
        meta = meta_by_id.get(lid) or {}
        labs_out.append({
            "id": lid,
            "lab_title": title,
            "state": meta.get("state"),
            "nodes": nodes,
            "links": links,
        })
        print(f"    {title}: nodes={len(nodes)} links={len(links)}", file=sys.stderr)
    return {"labs": labs_out}


def main(argv: Optional[list] = None) -> int:
    p = argparse.ArgumentParser(
        description="Pull CML lab topologies over the REST API (read-only).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--host", required=True, help="CML controller IP / hostname.")
    p.add_argument("--user", default="administrator",
                   help="CML username (default: administrator).")
    p.add_argument("--password", default=None,
                   help="CML password (or set the CML_PASSWORD env var).")
    p.add_argument("--verify-tls", action="store_true",
                   help="Enforce TLS cert verification (CML uses a self-signed cert by default).")
    p.add_argument("--lab", action="append", default=None,
                   help="Lab title or id to fetch. Repeat to select several. "
                        "Default: every lab on the controller.")
    p.add_argument("--out", default="cml_export.json",
                   help="Output JSON path (default: cml_export.json).")
    p.add_argument("--probe", action="store_true",
                   help="Authenticate and list labs, then exit without writing an export.")
    args = p.parse_args(argv)

    password = args.password or os.environ.get("CML_PASSWORD")
    if not password:
        print("[ERROR] CML password required: pass --password or set CML_PASSWORD.",
              file=sys.stderr)
        return 2

    client = CmlClient(args.host, verify_tls=args.verify_tls)
    try:
        client.login(args.user, password)
    except (urllib.error.URLError, RuntimeError, KeyError, TypeError, ValueError) as exc:
        print(f"[ERROR] CML login failed: {exc}", file=sys.stderr)
        return 1
    print(f"[ok] authenticated to {args.host} as {args.user}", file=sys.stderr)

    if args.probe:
        try:
            probe_access(client)
        except (urllib.error.URLError, RuntimeError, KeyError, TypeError, ValueError) as exc:
            print(f"[ERROR] CML access probe failed: {exc}", file=sys.stderr)
            return 1
        print("[ok] access probe succeeded (labs)", file=sys.stderr)
        return 0

    try:
        doc = fetch_labs(client, wanted=args.lab)
    except (urllib.error.URLError, RuntimeError, KeyError, TypeError, ValueError) as exc:
        print(f"[ERROR] fetch failed: {exc}", file=sys.stderr)
        return 1

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)
    print(f"[ok] wrote {args.out} ({len(doc['labs'])} lab(s))", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
