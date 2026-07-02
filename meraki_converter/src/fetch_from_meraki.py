# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""fetch_from_meraki.py — pull a Meraki org's config/topology over the Dashboard API.

Companion to ``convert.py``. The converter reads a local JSON file, but it is
convenient to grab the model straight from the Cisco Meraki Dashboard API v1 and
feed the result directly into the converter::

    python -m meraki_converter.src.fetch_from_meraki \\
        --org-id 669910444571368603 \\
        --out meraki_converter/Input_data/meraki_export.json
    # API key via --api-key or the MERAKI_API_KEY env var

    python -m meraki_converter.src.convert \\
        -i meraki_converter/Input_data/meraki_export.json \\
        -o meraki_converter/Output_data/ns_commands_meraki.txt

The API key comes from a CLI arg or the ``MERAKI_API_KEY`` environment variable;
nothing is hard-coded. Authentication is the v1 ``Authorization: Bearer`` header.
``api.meraki.com`` issues a 308 redirect to the org's shard (e.g. ``n190``);
that is followed automatically. Rate-limit responses (HTTP 429) are retried after
``Retry-After`` seconds, and list endpoints follow the ``Link`` header for
pagination.

Retrieval is READ-ONLY — only GET requests are issued, so a Sandbox/Observer
(read-only) API key is sufficient and the org is never modified. Standard library
only (urllib + ssl), no third-party dependencies.

Output is a single JSON document consumed by ``convert.py``::

    {
      "organizationId": "...",
      "organization": {...},
      "networks": [...],
      "devices": [...],
      "deviceStatuses": [...],
      "managementInterfaces": {serial: {...}},
      "switchPorts": {serial: [...]},
      "networkDetails": {networkId: {applianceVlans, appliancePorts,
                                     applianceSettings, wirelessSsids,
                                     topologyLinkLayer, clients}}
    }
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

BASE = "https://api.meraki.com/api/v1"


class _AllowAllRedirects(urllib.request.HTTPRedirectHandler):
    """Follow 308 (Permanent Redirect) like a 307 — older urllib stops on 308,
    and ``api.meraki.com`` uses 308 to send clients to their data-centre shard."""

    def http_error_308(self, req, fp, code, msg, headers):  # noqa: N802
        return self.http_error_307(req, fp, code, msg, headers)


class MerakiClient:
    """Minimal read-only Meraki Dashboard API v1 client (Bearer auth)."""

    def __init__(self, api_key: str, verify_tls: bool = True,
                 timeout: int = 30, max_retries: int = 4) -> None:
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        if verify_tls:
            ctx: Optional[ssl.SSLContext] = None
        else:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        handlers: List[urllib.request.BaseHandler] = [_AllowAllRedirects()]
        if ctx is not None:
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
        self._opener = urllib.request.build_opener(*handlers)

    def _raw(self, url: str) -> Tuple[int, Dict[str, str], bytes]:
        req = urllib.request.Request(url, method="GET", headers={
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "network-sketcher-meraki-converter/1.0",
        })
        attempt = 0
        while True:
            try:
                with self._opener.open(req, timeout=self.timeout) as resp:
                    return resp.status, dict(resp.headers), resp.read()
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt < self.max_retries:
                    wait = int(exc.headers.get("Retry-After", "1") or "1")
                    print(f"    [429] rate-limited; retrying in {wait}s", file=sys.stderr)
                    time.sleep(max(1, wait))
                    attempt += 1
                    continue
                # Re-raise as a RuntimeError carrying the API's error text.
                body = exc.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"HTTP {exc.code}: {body}") from exc

    def get(self, path: str) -> Any:
        """GET one resource (no pagination). ``path`` is relative to BASE."""
        _, _, body = self._raw(BASE + path)
        return json.loads(body.decode("utf-8", errors="replace")) if body else None

    def get_paginated(self, path: str, per_page: int = 1000) -> List[Any]:
        """GET a list endpoint, following the RFC5988 ``Link: rel=next`` header."""
        sep = "&" if "?" in path else "?"
        url: Optional[str] = f"{BASE}{path}{sep}perPage={per_page}"
        out: List[Any] = []
        while url:
            _, headers, body = self._raw(url)
            page = json.loads(body.decode("utf-8", errors="replace")) if body else []
            if isinstance(page, list):
                out.extend(page)
            else:  # endpoint returned an object instead of a list
                return page  # type: ignore[return-value]
            url = _next_link(headers.get("Link") or headers.get("link"))
        return out


def _next_link(link_header: Optional[str]) -> Optional[str]:
    """Extract the ``rel=next`` URL from an RFC5988 Link header, if present."""
    if not link_header:
        return None
    for part in link_header.split(","):
        seg = part.strip()
        if 'rel=next' in seg.replace('"', '').replace(' ', ''):
            lt, gt = seg.find("<"), seg.find(">")
            if 0 <= lt < gt:
                return seg[lt + 1:gt]
    return None


def fetch_org(client: MerakiClient, org_id: str,
              network_ids: Optional[List[str]] = None,
              include_clients: bool = False) -> Dict[str, Any]:
    """Pull a config-export-equivalent document for one organization.

    A failed sub-request is logged and skipped (never aborts the run) so we keep
    whatever we did get — e.g. ``appliance/vlans`` returns HTTP 400 ("VLANs are
    not enabled for this network") on flat networks, which is expected.
    """
    def grab(label: str, fn) -> Any:
        try:
            data = fn()
        except (urllib.error.URLError, RuntimeError, ValueError) as exc:
            print(f"  {label}: [skipped: {exc}]", file=sys.stderr)
            return None
        n = len(data) if isinstance(data, (list, dict)) else "?"
        print(f"  {label}: {n}", file=sys.stderr)
        return data

    out: Dict[str, Any] = {"organizationId": org_id}
    out["organization"] = grab("organization", lambda: client.get(f"/organizations/{org_id}"))
    networks = grab("networks", lambda: client.get_paginated(f"/organizations/{org_id}/networks")) or []
    if network_ids:
        networks = [n for n in networks if n.get("id") in set(network_ids)]
    out["networks"] = networks
    out["devices"] = grab("devices", lambda: client.get_paginated(f"/organizations/{org_id}/devices")) or []
    out["deviceStatuses"] = grab(
        "deviceStatuses", lambda: client.get_paginated(f"/organizations/{org_id}/devices/statuses")) or []

    # Org-wide appliance uplink (WAN) status — HA role + per-uplink reachability.
    out["applianceUplinkStatuses"] = grab(
        "applianceUplinkStatuses",
        lambda: client.get_paginated(f"/organizations/{org_id}/appliance/uplink/statuses")) or []

    # Per-device: management interface (all), real LLDP/CDP neighbours (all),
    # switch ports + switch L3 routing (switches only).
    mgmt: Dict[str, Any] = {}
    sw_ports: Dict[str, Any] = {}
    sw_port_status: Dict[str, Any] = {}
    lldp_cdp: Dict[str, Any] = {}
    sw_routing: Dict[str, Any] = {}
    sw_static: Dict[str, Any] = {}
    for dev in out["devices"]:
        serial = dev.get("serial")
        if not serial:
            continue
        mi = grab(f"mgmtInterface[{serial}]", lambda s=serial: client.get(f"/devices/{s}/managementInterface"))
        if mi and "errors" not in mi:
            mgmt[serial] = mi
        lc = grab(f"lldpCdp[{serial}]", lambda s=serial: client.get(f"/devices/{s}/lldpCdp"))
        if isinstance(lc, dict) and lc.get("ports"):
            lldp_cdp[serial] = lc
        if dev.get("productType") == "switch":
            sp = grab(f"switchPorts[{serial}]", lambda s=serial: client.get(f"/devices/{s}/switch/ports"))
            if isinstance(sp, list):
                sw_ports[serial] = sp
            st = grab(f"switchPortStatuses[{serial}]",
                      lambda s=serial: client.get(f"/devices/{s}/switch/ports/statuses"))
            if isinstance(st, list) and st:
                sw_port_status[serial] = st
            ri = grab(f"switchRouting[{serial}]",
                      lambda s=serial: client.get(f"/devices/{s}/switch/routing/interfaces"))
            if isinstance(ri, list) and ri:
                sw_routing[serial] = ri
            sr = grab(f"switchStaticRoutes[{serial}]",
                      lambda s=serial: client.get(f"/devices/{s}/switch/routing/staticRoutes"))
            if isinstance(sr, list) and sr:
                sw_static[serial] = sr
    out["managementInterfaces"] = mgmt
    out["switchPorts"] = sw_ports
    out["switchPortStatuses"] = sw_port_status
    out["lldpCdp"] = lldp_cdp
    out["switchRouting"] = sw_routing
    out["switchStaticRoutes"] = sw_static

    # Per-network detail (graceful: each piece may be absent for a given network).
    net_details: Dict[str, Any] = {}
    for net in networks:
        nid = net.get("id")
        if not nid:
            continue
        ptypes = set(net.get("productTypes") or [])
        detail: Dict[str, Any] = {}
        if "appliance" in ptypes:
            detail["applianceVlans"] = grab(
                f"applianceVlans[{nid}]", lambda n=nid: client.get(f"/networks/{n}/appliance/vlans"))
            # When VLANs are disabled the LAN gateway (L3) lives here, not in /vlans.
            detail["applianceSingleLan"] = grab(
                f"applianceSingleLan[{nid}]", lambda n=nid: client.get(f"/networks/{n}/appliance/singleLan"))
            detail["appliancePorts"] = grab(
                f"appliancePorts[{nid}]", lambda n=nid: client.get(f"/networks/{n}/appliance/ports"))
            detail["applianceStaticRoutes"] = grab(
                f"applianceStaticRoutes[{nid}]", lambda n=nid: client.get(f"/networks/{n}/appliance/staticRoutes"))
            detail["applianceSettings"] = grab(
                f"applianceSettings[{nid}]", lambda n=nid: client.get(f"/networks/{n}/appliance/settings"))
            detail["applianceWarmSpare"] = grab(
                f"applianceWarmSpare[{nid}]", lambda n=nid: client.get(f"/networks/{n}/appliance/warmSpare"))
            detail["applianceVpnSiteToSite"] = grab(
                f"applianceVpnSiteToSite[{nid}]",
                lambda n=nid: client.get(f"/networks/{n}/appliance/vpn/siteToSiteVpn"))
            detail["applianceVpnBgp"] = grab(
                f"applianceVpnBgp[{nid}]", lambda n=nid: client.get(f"/networks/{n}/appliance/vpn/bgp"))
        if "switch" in ptypes:
            detail["switchLinkAggregations"] = grab(
                f"switchLinkAggregations[{nid}]",
                lambda n=nid: client.get(f"/networks/{n}/switch/linkAggregations"))
        if "wireless" in ptypes:
            detail["wirelessSsids"] = grab(
                f"wirelessSsids[{nid}]", lambda n=nid: client.get(f"/networks/{n}/wireless/ssids"))
        detail["topologyLinkLayer"] = grab(
            f"topologyLinkLayer[{nid}]", lambda n=nid: client.get(f"/networks/{n}/topology/linkLayer"))
        if include_clients:
            detail["clients"] = grab(
                f"clients[{nid}]", lambda n=nid: client.get_paginated(f"/networks/{n}/clients?timespan=86400"))
        net_details[nid] = detail
    out["networkDetails"] = net_details
    return out


def main(argv: Optional[list] = None) -> int:
    p = argparse.ArgumentParser(
        description="Pull a Meraki organization model over the Dashboard API v1 (read-only).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--org-id", required=True, help="Meraki organization ID.")
    p.add_argument("--api-key", default=None,
                   help="Meraki API key (or set the MERAKI_API_KEY env var).")
    p.add_argument("--network-id", action="append", default=None, metavar="NET_ID",
                   help="Restrict to this network ID (repeatable). Default: all networks in the org.")
    p.add_argument("--include-clients", action="store_true",
                   help="Also pull network clients (drawn as endpoints). Off by default.")
    p.add_argument("--no-verify-tls", action="store_true",
                   help="Disable TLS certificate verification (not normally needed).")
    p.add_argument("--out", default="meraki_export.json",
                   help="Output JSON path (default: meraki_export.json).")
    args = p.parse_args(argv)

    api_key = args.api_key or os.environ.get("MERAKI_API_KEY")
    if not api_key:
        print("[ERROR] Meraki API key required: pass --api-key or set MERAKI_API_KEY.",
              file=sys.stderr)
        return 2

    client = MerakiClient(api_key, verify_tls=not args.no_verify_tls)
    print(f"[1/1] Fetching org {args.org_id} from {BASE} ...", file=sys.stderr)
    try:
        doc = fetch_org(client, args.org_id, network_ids=args.network_id,
                        include_clients=args.include_clients)
    except (urllib.error.URLError, RuntimeError) as exc:
        print(f"[ERROR] fetch failed: {exc}", file=sys.stderr)
        return 1

    out_path = args.out
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, ensure_ascii=False)
    print(f"[ok] wrote {out_path} "
          f"(networks={len(doc.get('networks', []))}, devices={len(doc.get('devices', []))})",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
