# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""fetch_from_netbox.py — pull a NetBox model over the read-only REST API.

Companion to ``convert.py``. The converter reads local files, but it is
convenient to grab the model straight from a reachable NetBox instance via its
REST API and feed the result directly into the converter.

It authenticates with a NetBox **API token** (``Authorization: Token <key>`` —
NetBox's native scheme; ``--bearer`` switches to ``Authorization: Bearer`` for
instances that require it), then pulls only the **core DCIM / IPAM** collections
the converter needs (deliberately no plugin- or custom-field-specific
endpoints, for portability across any NetBox), following NetBox's ``next``
pagination links until every object is retrieved. The result is written as a
single *combined JSON* that ``convert.py`` consumes::

    NETBOX_TOKEN=... python -m netbox_converter.src.fetch_from_netbox \\
        --url https://demo.netbox.dev \\
        --out netbox_converter/Input_data/netbox_export.json

    python -m netbox_converter.src.convert \\
        -i netbox_converter/Input_data/netbox_export.json \\
        -o netbox_converter/Output_data/ns_commands.txt

The token comes from a CLI arg or the ``NETBOX_TOKEN`` environment variable;
nothing is hard-coded. Public NetBox instances present a valid certificate, so
TLS verification is **on by default** (``--no-verify-tls`` to disable it for a
lab instance with a self-signed cert).

Retrieval is read-only (GET only) — this tool never modifies NetBox. Standard
library only (urllib + ssl), no third-party dependencies.
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple


# Core DCIM / IPAM collections pulled by default. (label, api_path, paginated).
# Intentionally limited to endpoints present in EVERY NetBox — no plugin or
# custom-field routes — so the converter stays portable. `status` is a single
# object (not paginated) used only to record the NetBox version in _meta.
_COLLECTIONS: List[Tuple[str, str, bool]] = [
    ("sites", "dcim/sites/", True),
    ("locations", "dcim/locations/", True),
    ("device_roles", "dcim/device-roles/", True),
    ("platforms", "dcim/platforms/", True),
    ("manufacturers", "dcim/manufacturers/", True),
    ("devices", "dcim/devices/", True),
    ("interfaces", "dcim/interfaces/", True),
    ("cables", "dcim/cables/", True),
    ("ip_addresses", "ipam/ip-addresses/", True),
    ("vlans", "ipam/vlans/", True),
    ("vrfs", "ipam/vrfs/", True),
    ("prefixes", "ipam/prefixes/", True),
]


class NetboxClient:
    """Minimal read-only NetBox REST client (token auth, follows pagination)."""

    def __init__(self, url: str, token: str, verify_tls: bool = True,
                 timeout: int = 60, bearer: bool = False, page_size: int = 500) -> None:
        base = url if url.startswith("http") else f"https://{url}"
        self.base = base.rstrip("/")
        self.api = f"{self.base}/api"
        self.timeout = timeout
        self.page_size = page_size
        scheme = "Bearer" if bearer else "Token"
        self.headers = {
            "Authorization": f"{scheme} {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if verify_tls:
            self.ctx: Optional[ssl.SSLContext] = None
        else:
            self.ctx = ssl.create_default_context()
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE

    def _get(self, url: str) -> Any:
        req = urllib.request.Request(url, headers=self.headers, method="GET")
        with urllib.request.urlopen(req, context=self.ctx, timeout=self.timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw.strip() else None

    def get_object(self, path: str) -> Any:
        """GET a single (non-paginated) object, e.g. ``status/``."""
        return self._get(f"{self.api}/{path.lstrip('/')}")

    def get_list(self, path: str, params: Optional[Dict[str, str]] = None) -> List[dict]:
        """GET a paginated collection, following ``next`` links to completion."""
        query = dict(params or {})
        query.setdefault("limit", str(self.page_size))
        url: Optional[str] = f"{self.api}/{path.lstrip('/')}?{urllib.parse.urlencode(query)}"
        results: List[dict] = []
        while url:
            page = self._get(url)
            if not isinstance(page, dict):
                break
            results.extend(page.get("results", []) or [])
            url = page.get("next")  # absolute URL or null
        return results


def fetch_model(client: NetboxClient,
                site_filter: Optional[List[str]] = None) -> Dict[str, Any]:
    """Return a combined-JSON document ready for ``convert.py``.

    Pulls every core DCIM / IPAM collection. A failed query is logged and
    skipped (never aborts the run), so a partial fetch still produces output.
    ``site_filter`` (site slugs) scopes the object-heavy collections that
    support a site filter; reference tables (roles/platforms/etc.) are always
    pulled whole.
    """
    status = None
    try:
        status = client.get_object("status/")
    except (urllib.error.URLError, ValueError) as exc:
        print(f"  status: [skipped: {exc}]", file=sys.stderr)

    out: Dict[str, Any] = {
        "_meta": {
            "source": "NetBox",
            "url": client.base,
            "netbox_version": (status or {}).get("netbox-version"),
            "site_filter": site_filter or [],
            "counts": {},
        },
    }

    # Site-scoped filtering: NetBox filters these by site slug (repeatable).
    # Cables have no site filter, so they are always fetched whole and later
    # narrowed by the reader to interfaces of in-scope devices.
    site_scoped = {"devices", "interfaces", "ip_addresses"}
    site_params: Dict[str, str] = {}
    if site_filter:
        # urlencode collapses duplicate keys; NetBox accepts repeated ?site=,
        # but for the common single-site case one value is enough. For multiple
        # sites we fall back to no filter (pull all) to stay correct.
        if len(site_filter) == 1:
            site_params = {"site": site_filter[0]}
        else:
            print("  [note] multiple --site values: pulling all sites "
                  "(reader will scope).", file=sys.stderr)

    for label, path, paginated in _COLLECTIONS:
        params = site_params if (label in site_scoped and site_params) else None
        try:
            if paginated:
                data = client.get_list(path, params)
            else:
                data = client.get_object(path)
        except (urllib.error.URLError, ValueError) as exc:
            print(f"  {label}: [skipped: {exc}]", file=sys.stderr)
            data = [] if paginated else None
        out[label] = data
        n = len(data) if isinstance(data, list) else (1 if data else 0)
        out["_meta"]["counts"][label] = n
        print(f"  {label}: {n}", file=sys.stderr)

    return out


def main(argv: Optional[list] = None) -> int:
    p = argparse.ArgumentParser(
        description="Pull a NetBox model over the read-only REST API (DCIM/IPAM core only).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--url", required=True,
                   help="NetBox base URL, e.g. https://demo.netbox.dev")
    p.add_argument("--token", default=None,
                   help="NetBox API token (or set the NETBOX_TOKEN env var).")
    p.add_argument("--bearer", action="store_true",
                   help="Use 'Authorization: Bearer' instead of NetBox's native 'Token' scheme.")
    p.add_argument("--site", action="append", default=None,
                   help="Limit to this site slug (repeatable). Default: all sites.")
    p.add_argument("--page-size", type=int, default=500,
                   help="REST pagination page size (default: 500).")
    p.add_argument("--no-verify-tls", action="store_true",
                   help="Disable TLS cert verification (for lab instances with self-signed certs).")
    p.add_argument("--out", default="netbox_export.json",
                   help="Output JSON path (default: netbox_export.json).")
    args = p.parse_args(argv)

    token = args.token or os.environ.get("NETBOX_TOKEN")
    if not token:
        print("[ERROR] NetBox API token required: pass --token or set NETBOX_TOKEN.",
              file=sys.stderr)
        return 2

    client = NetboxClient(
        args.url, token,
        verify_tls=not args.no_verify_tls,
        bearer=args.bearer,
        page_size=args.page_size,
    )

    # Fail fast with a clear message if auth / connectivity is broken.
    try:
        status = client.get_object("status/")
        ver = (status or {}).get("netbox-version", "?")
        print(f"[ok] connected to {client.base} (NetBox {ver})", file=sys.stderr)
    except urllib.error.HTTPError as exc:
        hint = " — check the API token" if exc.code in (401, 403) else ""
        print(f"[ERROR] NetBox request failed: HTTP {exc.code}{hint}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, ValueError) as exc:
        print(f"[ERROR] cannot reach NetBox at {client.base}: {exc}", file=sys.stderr)
        return 1

    doc = fetch_model(client, site_filter=args.site)

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, ensure_ascii=False)
    counts = doc["_meta"]["counts"]
    print(f"[ok] wrote {args.out} "
          f"(devices={counts.get('devices', 0)}, interfaces={counts.get('interfaces', 0)}, "
          f"cables={counts.get('cables', 0)}, ip_addresses={counts.get('ip_addresses', 0)})",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
