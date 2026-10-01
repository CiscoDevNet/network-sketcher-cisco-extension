# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""fetch_from_vmanage.py -- pull a Cisco Catalyst SD-WAN Manager (vManage)
device/interface model over its REST API.

Companion to ``convert.py``. The converter reads local files, but in a lab it
is convenient to grab the model straight from a reachable vManage and feed the
result directly into the converter.

It authenticates with vManage's form-based login (``POST /j_security_check``
with ``j_username``/``j_password``, session cookie), then fetches the CSRF
token (``GET /dataservice/client/token``) required on every subsequent
request, then pulls the device inventory (``/dataservice/device``) and, per
device, its interface state (``/dataservice/device/interface``), WAN
transport-colour state (``/dataservice/device/control/waninterface``), and --
best-effort, the PREFERRED source for the underlay's L1 links when available
-- observed neighbor adjacency (``/dataservice/device/lldp/neighbors`` and
``/dataservice/device/cdp/neighbors``) -- plus, per Edge device, its live BFD
tunnel session state (``/dataservice/device/bfd/sessions``), the observed
source ``sdwan_physical_mapper.py`` uses to annotate each UNDERLAY transport
segment's mesh shape (Full Mesh / Hub-and-Spoke / Partial Mesh), and its
control-plane connections to the controllers
(``/dataservice/device/control/synced/connections``, falling back to the live
``/dataservice/device/control/connections``, plus
``/dataservice/device/control/connectionshistory`` for the torn-down vBond
sessions that appear nowhere else). Finally it
scans the feature-template library (``/dataservice/template/feature``) once,
fabric-wide, to resolve each Service VPN id to its configured NAME
(``10`` -> ``Corporate``) for the OVERLAY's waypoint clouds. The result is a
single combined JSON that ``convert.py`` consumes::

    LLDP/CDP note: neither endpoint is documented in the on-box Swagger spec
    (``/apidocs``) or in Cisco's public "Device Realtime Monitoring" API
    reference at the time this was written, and both returned HTTP 404 in
    the one CML-simulated vManage lab this fetch script has been validated
    against. They are attempted anyway (tolerating 404/empty exactly like
    every other per-device query below) because they may work on a real
    physical vManage deployment -- see ``sdwan_physical_mapper.py`` and the
    README's accuracy caveats for how the (UNVERIFIED-schema) response is
    parsed defensively once you have one.

    python -m sdwan_converter.src.fetch_from_vmanage \\
        --host vmanage.example.com --user admin \\
        --out sdwan_converter/Input_data/vmanage_export.json
    # password via --password or the VMANAGE_PASSWORD env var

    python -m sdwan_converter.src.convert \\
        -i sdwan_converter/Input_data/vmanage_export.json -m both \\
        -o sdwan_converter/Output_data/ns_commands.txt

Credentials come from a CLI arg or the ``VMANAGE_PASSWORD`` environment
variable; nothing is hard-coded and the password is never logged. vManage
typically uses a self-signed certificate in a lab, so TLS verification is
disabled by default (``--verify-tls`` to enforce it).

Retrieval is read-only (GET only, plus the one unavoidable login POST) --
this tool never modifies the SD-WAN fabric. Standard library only (urllib +
http.cookiejar + ssl), no third-party dependencies.
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple


class VmanageClient:
    """Minimal read-only vManage REST client (form login + CSRF token)."""

    def __init__(self, host: str, verify_tls: bool = False, timeout: int = 30) -> None:
        self.base = host if host.startswith("http") else f"https://{host}"
        self.timeout = timeout
        self.token: Optional[str] = None
        self.cookie_jar = http.cookiejar.CookieJar()
        handlers = [urllib.request.HTTPCookieProcessor(self.cookie_jar)]
        if not verify_tls:
            print("[WARN] TLS certificate verification is DISABLED: the credentials and the "
                  "session token traverse an unverified TLS connection. Pass --verify-tls "
                  "against anything other than a lab vManage.", file=sys.stderr)
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
        self.opener = urllib.request.build_opener(*handlers)

    def _open(self, req: urllib.request.Request) -> str:
        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code} from {req.full_url}: {detail[:500]}") from exc

    def login(self, user: str, password: str) -> None:
        """Form-based login (session cookie), then fetch the CSRF token
        required on every subsequent request. Neither the password nor the
        token is ever printed/logged."""
        data = urllib.parse.urlencode({"j_username": user, "j_password": password}).encode()
        req = urllib.request.Request(self.base + "/j_security_check", data=data, method="POST")
        body = self._open(req)
        if "<html" in body.lower():
            # vManage returns the login page HTML (HTTP 200) on bad credentials
            # instead of an HTTP error -- detect it explicitly.
            raise RuntimeError("login failed: vManage returned the login page "
                               "(check host/user/password)")

        token_req = urllib.request.Request(self.base + "/dataservice/client/token", method="GET")
        token = self._open(token_req).strip()
        if not token or "<html" in token.lower():
            raise RuntimeError("login succeeded but no CSRF token was returned")
        self.token = token

    def get(self, path: str) -> Any:
        req = urllib.request.Request(
            self.base + path,
            headers={"X-XSRF-TOKEN": self.token or "", "Accept": "application/json"},
            method="GET",
        )
        raw = self._open(req)
        return json.loads(raw) if raw.strip() else None


# Feature-template types that carry a Service VPN definition. ``cisco_vpn``
# is the IOS-XE (cEdge) template; ``vpn-vedge`` is the Viptela-OS (vEdge) one.
_VPN_TEMPLATE_TYPES = ("cisco_vpn", "vpn-vedge")


def _constant_value(blob: Any) -> Optional[Any]:
    """Unwrap a vManage template-definition field, but ONLY when it holds a
    literal value.

    A ``templateDefinition`` field looks like
    ``{"vipType": "constant", "vipValue": 10}``. Any other ``vipType``
    (``variableName`` -- filled in per device at attach time --,
    ``ignore``, ``notIgnore``, ...) means the template itself does not pin
    the value down, so it cannot be used to name a VPN fabric-wide.
    """
    if not isinstance(blob, dict):
        return None
    if str(blob.get("vipType") or "").strip().lower() != "constant":
        return None
    value = blob.get("vipValue")
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return value


def fetch_vpn_names(client: VmanageClient) -> Dict[str, str]:
    """Resolve Service VPN id -> configured VPN name by scanning EVERY VPN
    feature template in the library (``/dataservice/template/feature``).

    Returns e.g. ``{"10": "Corporate", "11": "PCI", "12": "Guest"}`` -- keys
    are always STRINGS, even though this API reports ``vpn-id`` as an int
    (``/dataservice/device/interface`` reports it as a string, so the reader
    and mappers normalise to string keys throughout). Reserved ids (0, 512,
    ...) are returned too if the fabric defines them; filtering them out is
    the Overlay mapper's job, not this function's.

    A fabric-wide scan is used DELIBERATELY, in preference to walking each
    device's own attached device-template. That per-device chain was verified
    to break on a live vManage: some devices report a device-template name
    that does not exist under ``/dataservice/template/device`` at all, and
    ``/dataservice/template/device/config/attached/{id}`` returns empty for
    every device in a Config-Group-managed fabric. Scanning the template
    library instead needs no per-device join.

    When two templates pin the SAME ``vpn-id`` to different names, the winner
    is chosen by: a user-defined template beats a ``factoryDefault`` one,
    then the higher ``attachedMastersCount`` (more device templates use it),
    then the alphabetically first ``templateName``.

    Entirely BEST-EFFORT: every failure (endpoint missing, HTTP error,
    malformed body) is reported and skipped, and an empty dict is returned
    rather than raising -- the Overlay then falls back to ``VPN <id>`` labels.
    """
    try:
        resp = client.get("/dataservice/template/feature")
    except (urllib.error.URLError, RuntimeError, ValueError) as exc:
        print(f"    feature templates: [skipped: {exc}]", file=sys.stderr)
        return {}

    rows = resp.get("data") if isinstance(resp, dict) else None
    if not isinstance(rows, list):
        print("    feature templates: [skipped: no 'data' list in response]", file=sys.stderr)
        return {}

    names: Dict[str, str] = {}
    # vpn-id -> the winning template's precedence key (lower sorts better).
    best: Dict[str, Tuple[int, int, str]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("templateType") or "").strip() not in _VPN_TEMPLATE_TYPES:
            continue
        template_id = str(row.get("templateId") or "").strip()
        if not template_id:
            continue
        try:
            obj = client.get(f"/dataservice/template/feature/object/{template_id}")
        except (urllib.error.URLError, RuntimeError, ValueError) as exc:
            print(f"    feature template {template_id}: [skipped: {exc}]", file=sys.stderr)
            continue
        definition = obj.get("templateDefinition") if isinstance(obj, dict) else None
        if not isinstance(definition, dict):
            continue
        vpn_id = _constant_value(definition.get("vpn-id"))
        vpn_name = _constant_value(definition.get("name"))
        if vpn_id is None or vpn_name is None:
            continue
        key = str(vpn_id).strip()
        label = str(vpn_name).strip()
        if not key or not label:
            continue

        try:
            attached = int(row.get("attachedMastersCount") or 0)
        except (TypeError, ValueError):
            attached = 0
        rank = (
            1 if bool(row.get("factoryDefault")) else 0,
            -attached,
            str(row.get("templateName") or ""),
        )
        if key not in best or rank < best[key]:
            best[key] = rank
            names[key] = label
    return names


def fetch_model(client: VmanageClient) -> Dict[str, Any]:
    """Return a combined-JSON document ready for ``convert.py``.

    Pulls the device inventory, then per device (by ``system-ip``) its
    interface state, WAN transport-colour state, and (best-effort, PREFERRED
    for the underlay's L1 links when it works) observed LLDP/CDP neighbor
    adjacency. A failed per-device query is logged and skipped (never aborts
    the run), so a partial fetch still produces the best diagram the data
    allows -- this is exactly how the CML-simulated lab this fetch script has
    been validated against behaves today, since its vManage returns HTTP 404
    for both neighbor endpoints (they are not documented in that platform's
    on-box Swagger spec either); a real physical vManage deployment may
    actually populate them, which is the whole point of attempting them here.

    Finally, ONE fabric-wide (not per-device) pass resolves Service VPN
    id -> name from the feature-template library -- see
    :func:`fetch_vpn_names`, which is equally best-effort.

    Each ``/dataservice/device`` record is stored VERBATIM, so a live export
    already carries every field the mappers use -- including ``site-name``
    (``site-id 3`` -> ``"br1"``), which both mappers prefer over ``site<id>``
    as the NS area name (see ``sdwan_topology.build_site_area_map``). No extra
    endpoint is needed for it: the ``/dataservice/site*`` paths returned HTTP
    404 on the vManage this was validated against, and
    ``/dataservice/template/policy/list/site`` disagreed with ``site-name``
    (and omitted some sites), so neither is used.

    NOT fetched, deliberately: ``/dataservice/device/vpn``, which returned an
    empty ``"data": []`` array on every request during validation and is
    therefore not a usable Service VPN source. Service VPN membership comes
    from each interface record's ``vpn-id`` instead.
    """
    out: Dict[str, Any] = {
        "_meta": {"source": "Cisco SD-WAN Manager (vManage)", "host": client.base},
        "devices": [], "interfaces": {}, "wan_interfaces": {},
        "lldp_neighbors": {}, "cdp_neighbors": {}, "bfd_sessions": {},
        "control_connections": {}, "control_connections_history": {},
        "vpn_names": {},
    }

    def _grab(path: str, label: str) -> Any:
        try:
            return client.get(path)
        except (urllib.error.URLError, RuntimeError, ValueError) as exc:
            print(f"    {label}: [skipped: {exc}]", file=sys.stderr)
            return None

    resp = _grab("/dataservice/device", "device inventory")
    out["devices"] = (resp or {}).get("data", []) if isinstance(resp, dict) else []
    print(f"  devices: {len(out['devices'])}", file=sys.stderr)

    for dev in out["devices"]:
        sysip = dev.get("system-ip")
        if not sysip:
            continue
        itf = _grab(f"/dataservice/device/interface?deviceId={sysip}", f"interfaces {sysip}")
        out["interfaces"][sysip] = (itf or {}).get("data", []) if isinstance(itf, dict) else []
        wan = _grab(f"/dataservice/device/control/waninterface?deviceId={sysip}",
                    f"waninterface {sysip}")
        out["wan_interfaces"][sysip] = (wan or {}).get("data", []) if isinstance(wan, dict) else []

        # Best-effort, PREFERRED observed-adjacency source for the underlay's
        # L1 links (sdwan_physical_mapper.py's Pass 1) -- attempted for EVERY
        # device (Edge or controller alike), tolerating a 404/empty response
        # exactly like every other query on this page. LLDP is the universal
        # protocol (works on both vEdge/Viptela-OS and cEdge/IOS-XE); CDP is
        # attempted too since it is IOS-XE (cEdge) specific and a genuine
        # vEdge is simply expected to answer empty/404 to it, same as any
        # platform lacking the endpoint at all.
        lldp = _grab(f"/dataservice/device/lldp/neighbors?deviceId={sysip}",
                    f"lldp neighbors {sysip}")
        out["lldp_neighbors"][sysip] = (lldp or {}).get("data", []) if isinstance(lldp, dict) else []
        cdp = _grab(f"/dataservice/device/cdp/neighbors?deviceId={sysip}",
                    f"cdp neighbors {sysip}")
        out["cdp_neighbors"][sysip] = (cdp or {}).get("data", []) if isinstance(cdp, dict) else []

        # Best-effort observed SD-WAN fabric connectivity: live BFD
        # tunnel-session state, the source sdwan_physical_mapper.py uses to
        # annotate each UNDERLAY transport segment's mesh shape (Full Mesh /
        # Hub-and-Spoke / Partial Mesh). Only meaningful for SD-WAN Edge
        # devices (vSmart/vBond/
        # vManage do not run data-plane BFD tunnels), but is attempted for
        # every device and simply tolerates an empty/404 response like every
        # other query on this page, so no personality-based branching is
        # needed here.
        bfd = _grab(f"/dataservice/device/bfd/sessions?deviceId={sysip}",
                    f"bfd sessions {sysip}")
        out["bfd_sessions"][sysip] = (bfd or {}).get("data", []) if isinstance(bfd, dict) else []

        # Control-plane connectivity to the controllers, the source
        # sdwan_physical_mapper.py joins to its inferred transport segments to
        # draw the UNDERLAY's control-plane path. The CACHED twin
        # (control/synced/connections) is preferred: it answers from vManage's
        # own database instead of polling the device, which matters on a large
        # fabric; the live endpoint is only queried when it returns nothing.
        # Both REQUIRE deviceId -- a bare call answers HTTP 400 'Device data
        # error' -- so there is no fabric-wide variant to fetch once.
        conns = _grab(f"/dataservice/device/control/synced/connections?deviceId={sysip}",
                      f"control connections (synced) {sysip}")
        rows = (conns or {}).get("data", []) if isinstance(conns, dict) else []
        if not rows:
            conns = _grab(f"/dataservice/device/control/connections?deviceId={sysip}",
                          f"control connections {sysip}")
            rows = (conns or {}).get("data", []) if isinstance(conns, dict) else []
        out["control_connections"][sysip] = rows

        # A vBond's control connections are torn down once onboarding
        # completes, so they exist in the history endpoint ONLY (with
        # state='tear_down' and an unusable system-ip of 0.0.0.0, joinable to
        # the vBond by its private-ip/public-ip alone).
        hist = _grab(f"/dataservice/device/control/connectionshistory?deviceId={sysip}",
                     f"control connections history {sysip}")
        out["control_connections_history"][sysip] = (
            (hist or {}).get("data", []) if isinstance(hist, dict) else [])

    # Fabric-wide (NOT per-device): resolve each Service VPN id to its
    # configured name for the Overlay's waypoint clouds. Best-effort -- an
    # empty result just means the Overlay labels its clouds 'VPN <id>'.
    out["vpn_names"] = fetch_vpn_names(client)

    total_itf = sum(len(v) for v in out["interfaces"].values())
    total_wan = sum(len(v) for v in out["wan_interfaces"].values())
    total_lldp = sum(len(v) for v in out["lldp_neighbors"].values())
    total_cdp = sum(len(v) for v in out["cdp_neighbors"].values())
    total_bfd = sum(len(v) for v in out["bfd_sessions"].values())
    total_conn = sum(len(v) for v in out["control_connections"].values())
    total_hist = sum(len(v) for v in out["control_connections_history"].values())
    print(f"  interfaces={total_itf} wan_interfaces={total_wan} "
          f"lldp_neighbors={total_lldp} cdp_neighbors={total_cdp} "
          f"bfd_sessions={total_bfd} control_connections={total_conn} "
          f"control_connections_history={total_hist} "
          f"(across {len(out['devices'])} device(s)), "
          f"vpn_names={len(out['vpn_names'])}", file=sys.stderr)
    return out


def probe_access(client: VmanageClient) -> None:
    """Confirm that the authenticated account can read device inventory."""
    response = client.get("/dataservice/device")
    if not isinstance(response, dict) or not isinstance(response.get("data"), list):
        raise RuntimeError("device inventory returned an unexpected response")


def main(argv: Optional[list] = None) -> int:
    p = argparse.ArgumentParser(
        description="Pull a Cisco Catalyst SD-WAN Manager (vManage) device/interface model "
                     "over the REST API (read-only).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--host", required=True, help="vManage IP / hostname (e.g. vmanage.example.com:8443).")
    p.add_argument("--user", default="admin", help="vManage username (default: admin).")
    p.add_argument("--password", default=None,
                   help="vManage password (or set the VMANAGE_PASSWORD env var).")
    p.add_argument("--verify-tls", action="store_true",
                   help="Enforce TLS cert verification (vManage uses self-signed certs by default in a lab).")
    p.add_argument("--out", default="vmanage_export.json",
                   help="Output JSON path (default: vmanage_export.json).")
    p.add_argument("--probe", action="store_true",
                   help="Authenticate and perform one lightweight read-only API check, "
                        "then exit without writing an export.")
    args = p.parse_args(argv)

    password = args.password or os.environ.get("VMANAGE_PASSWORD")
    if not password:
        print("[ERROR] vManage password required: pass --password or set VMANAGE_PASSWORD.",
              file=sys.stderr)
        return 2

    client = VmanageClient(args.host, verify_tls=args.verify_tls)
    try:
        client.login(args.user, password)
    except (urllib.error.URLError, RuntimeError, KeyError, ValueError) as exc:
        print(f"[ERROR] vManage login failed: {exc}", file=sys.stderr)
        return 1
    print(f"[ok] authenticated to {args.host} as {args.user}", file=sys.stderr)

    if args.probe:
        try:
            probe_access(client)
        except (urllib.error.URLError, RuntimeError, KeyError, TypeError, ValueError) as exc:
            print(f"[ERROR] vManage access probe failed: {exc}", file=sys.stderr)
            return 1
        print("[ok] access probe succeeded (device inventory)", file=sys.stderr)
        return 0

    try:
        doc = fetch_model(client)
    except (urllib.error.URLError, RuntimeError) as exc:
        print(f"[ERROR] fetch failed: {exc}", file=sys.stderr)
        return 1

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, ensure_ascii=False)
    print(f"[ok] wrote {args.out} ({len(doc['devices'])} device(s))", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
