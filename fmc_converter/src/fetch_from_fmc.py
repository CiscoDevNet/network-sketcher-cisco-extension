# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""fetch_from_fmc.py — pull a Secure Firewall Management Center model.

Read-only. The password comes from ``--password`` or ``FMC_PASSWORD`` and is
never logged. FMC appliances use a private certificate, so TLS verification
is off unless ``--verify-tls`` is set.

    python -m fmc_converter.src.fetch_from_fmc \\
        --host fmc.example.com --user admin \\
        --out fmc_converter/Input_data/fmc_export.json
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
from urllib.parse import urlparse


class FmcClient:
    """Minimal read-only FMC REST client."""

    def __init__(self, host: str, verify_tls: bool = False, timeout: int = 60) -> None:
        base = host if host.startswith("http") else f"https://{host}"
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.token: Optional[str] = None
        self.domain: Optional[str] = None
        if verify_tls:
            self.ctx: Optional[ssl.SSLContext] = None
        else:
            self.ctx = ssl.create_default_context()
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE

    def _request(self, path: str, method: str = "GET") -> Any:
        url = path if path.startswith("http") else self.base + path
        headers = {"Accept": "application/json"}
        if self.token:
            headers["X-auth-access-token"] = self.token
        req = urllib.request.Request(url, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=self.timeout) as resp:
                payload = resp.read().decode("utf-8", errors="replace")
                response_headers = {key.lower(): value for key, value in resp.headers.items()}
                status = resp.status
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise RuntimeError(f"HTTP {exc.code} {urlparse(url).path}: {detail}") from exc
        body = json.loads(payload) if payload.strip() else None
        return status, response_headers, body

    def login(self, user: str, password: str) -> None:
        url = self.base + "/api/fmc_platform/v1/auth/generatetoken"
        basic = base64.b64encode(f"{user}:{password}".encode()).decode()
        req = urllib.request.Request(
            url, method="POST",
            headers={"Authorization": f"Basic {basic}", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=self.timeout) as resp:
                headers = {key.lower(): value for key, value in resp.headers.items()}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise RuntimeError(f"HTTP {exc.code} authentication failed: {detail}") from exc
        token = headers.get("x-auth-access-token", "")
        domain = headers.get("domain_uuid", "")
        if not token or not domain:
            raise RuntimeError("authentication returned no token or domain")
        self.token = token
        self.domain = domain

    def get(self, path: str) -> Any:
        _status, _headers, body = self._request(path)
        return body

    def items(self, path: str) -> List[Dict[str, Any]]:
        found: List[Dict[str, Any]] = []
        seen = set()
        while path and path not in seen:
            seen.add(path)
            body = self.get(path)
            if not isinstance(body, dict):
                break
            found.extend(item for item in (body.get("items") or []) if isinstance(item, dict))
            nxt = (body.get("paging") or {}).get("next")
            if isinstance(nxt, list) and nxt and isinstance(nxt[0], str):
                path = nxt[0]
            elif isinstance(nxt, str) and nxt:
                path = nxt
            else:
                path = ""
        return found


def probe_access(client: FmcClient) -> None:
    body = client.get("/api/fmc_platform/v1/info/serverversion")
    if not isinstance(body, dict) or not isinstance(body.get("items"), list):
        raise RuntimeError("serverversion returned an unexpected response")


def _fmc_identity(client: FmcClient) -> Dict[str, str]:
    body = client.get("/api/fmc_platform/v1/info/serverversion")
    item = {}
    if isinstance(body, dict) and body.get("items"):
        first = body["items"][0]
        if isinstance(first, dict):
            item = first
    hostname = str(item.get("hostname") or "")
    return {
        "name": str(item.get("name") or hostname or "FMC"),
        "hostname": hostname,
        "version": str(item.get("serverVersion") or ""),
        "ipv4": "",
        "gateway": "",
    }


def _enrich_route_networks(client: FmcClient, routes: List[Dict[str, Any]]) -> None:
    """Copy each destination object's value onto the route. any-ipv4 is 0.0.0.0/0."""
    if not client.domain:
        return
    cache: Dict[str, str] = {}
    for route in routes:
        nets = route.get("selectedNetworks") or []
        if isinstance(nets, dict):
            nets = [nets]
        for net in nets:
            if not isinstance(net, dict) or net.get("value") or not net.get("id"):
                continue
            object_id = str(net["id"])
            if object_id not in cache:
                path = (
                    f"/api/fmc_config/v1/domain/{client.domain}/object/networks/{object_id}"
                )
                try:
                    body = client.get(path)
                    cache[object_id] = str(body.get("value") or "") if isinstance(body, dict) else ""
                except (urllib.error.URLError, RuntimeError, ValueError, AttributeError):
                    cache[object_id] = ""
            if cache[object_id]:
                net["value"] = cache[object_id]


def fetch_model(client: FmcClient) -> Dict[str, Any]:
    if not client.domain:
        raise RuntimeError("not authenticated")
    domain = client.domain
    identity = _fmc_identity(client)
    devices = client.items(
        f"/api/fmc_config/v1/domain/{domain}/devices/devicerecords?expanded=true&limit=100"
    )
    try:
        pairs = client.items(
            f"/api/fmc_config/v1/domain/{domain}/devicehapairs/ftddevicehapairs?expanded=true&limit=100"
        )
    except (urllib.error.URLError, RuntimeError, ValueError) as exc:
        print(f"    ha pairs: [skipped: {exc}]", file=sys.stderr)
        pairs = []
    for device in devices:
        device_id = device.get("id")
        if not device_id:
            device["interfaces"] = []
            continue
        path = (
            f"/api/fmc_config/v1/domain/{domain}/devices/devicerecords/"
            f"{device_id}/ftdallinterfaces?expanded=true&limit=100"
        )
        try:
            device["interfaces"] = client.items(path)
        except (urllib.error.URLError, RuntimeError, ValueError) as exc:
            print(f"    interfaces {device.get('name')}: [skipped: {exc}]", file=sys.stderr)
            device["interfaces"] = []
        device["static_routes"] = []
        for family in ("ipv4", "ipv6"):
            route_path = (
                f"/api/fmc_config/v1/domain/{domain}/devices/devicerecords/"
                f"{device_id}/routing/{family}staticroutes?expanded=true&limit=100"
            )
            try:
                routes = client.items(route_path)
                _enrich_route_networks(client, routes)
            except (urllib.error.URLError, RuntimeError, ValueError) as exc:
                print(f"    {family} routes {device.get('name')}: [skipped: {exc}]",
                      file=sys.stderr)
                continue
            device["static_routes"].extend(routes)
        print(
            f"    {device.get('name')}: interfaces={len(device['interfaces'])} "
            f"routes={len(device['static_routes'])}",
            file=sys.stderr,
        )
    print(f"  devices={len(devices)} ha_pairs={len(pairs)}", file=sys.stderr)
    return {"_meta": {"source": "fmc", "host": client.base}, "fmc": identity,
            "devices": devices, "ha_pairs": pairs}


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Pull a Firewall Management Center model (read-only).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--host", required=True, help="FMC IP / hostname.")
    parser.add_argument("--user", default="admin", help="FMC username (default: admin).")
    parser.add_argument("--password", default=None,
                        help="FMC password (or set the FMC_PASSWORD env var).")
    parser.add_argument("--verify-tls", action="store_true",
                        help="Enforce TLS cert verification (default: off).")
    parser.add_argument("--out", default="fmc_export.json",
                        help="Output JSON path (default: fmc_export.json).")
    parser.add_argument("--probe", action="store_true",
                        help="Authenticate and read the server version, then exit "
                             "without writing an export.")
    args = parser.parse_args(argv)

    password = args.password or os.environ.get("FMC_PASSWORD")
    if not password:
        print("[ERROR] FMC password required: pass --password or set FMC_PASSWORD.",
              file=sys.stderr)
        return 2
    client = FmcClient(args.host, verify_tls=args.verify_tls)
    try:
        client.login(args.user, password)
    except (urllib.error.URLError, RuntimeError, KeyError, ValueError) as exc:
        print(f"[ERROR] FMC login failed: {exc}", file=sys.stderr)
        return 1
    print(f"[ok] authenticated to {args.host} as {args.user}", file=sys.stderr)
    if args.probe:
        try:
            probe_access(client)
        except (urllib.error.URLError, RuntimeError, KeyError, TypeError, ValueError) as exc:
            print(f"[ERROR] FMC access probe failed: {exc}", file=sys.stderr)
            return 1
        print("[ok] access probe succeeded (serverversion)", file=sys.stderr)
        return 0
    try:
        document = fetch_model(client)
    except (urllib.error.URLError, RuntimeError, KeyError, TypeError, ValueError) as exc:
        print(f"[ERROR] fetch failed: {exc}", file=sys.stderr)
        return 1
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(document, fh, indent=1, ensure_ascii=False)
    print(f"[ok] wrote {args.out} ({len(document['devices'])} device(s))", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
