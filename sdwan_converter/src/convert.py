# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""convert.py -- Cisco Catalyst SD-WAN (vManage) export -> Network Sketcher
command converter.

Reads a vManage export (a single combined ``*.json``, or a directory of
per-endpoint ``*.json`` dumps) and emits ready-to-use Network Sketcher command
scripts in two selectable modes:

  * ``underlay`` -- the physical/circuit view: every managed device
    (vManage/vSmart/vBond/Edge) plus INFERRED VPN0 transport-circuit links
    (every Edge on a given transport colour is grouped into one gray cloud
    named after that colour -- "biz-internet" -- regardless of IPv4 subnet;
    the cloud carries an L2 segment named after the colour on its OWN
    'Dummy N' ports only, which is what makes the underlay's L2/L3 diagrams
    show one broadcast domain per transport).
  * ``overlay``  -- the Service VPN (VRF/LAN) view: SD-WAN Edges only, each
    split into a WAN side (a synthetic ``Vpn <id>`` port per Service VPN,
    linked UP to that VPN's purple waypoint cloud, which carries the VPN name
    as its L2 segment) and a LAN side (its real physical Service VPN
    interfaces, with their IPs, linked DOWN to one synthetic per-Edge
    ``Dummy_<n>`` LAN switch). Every port on both sides gets the VPN's VRF as
    its L3 instance.
    Neither mode draws the IPsec+BFD tunnel-to-tunnel mesh as links (the
    underlay only ANNOTATES each transport segment's observed mesh shape) or
    OMP route state -- see the README for the full scope statement.

Usage (run as a module from the repository root)::

    python -m sdwan_converter.src.convert \\
        --input  vmanage_export.json \\
        [--mode  underlay|overlay|both]  \\
        [--out   ns_commands.txt]        \\
        [--config sdwan_to_ns_config.json] \\
        [--layout tier]

Outputs (written to the same directory as ``--out``):

  underlay : <stem>_sdwan_underlay.txt, ns_model_underlay.json,
             sdwan_inventory.csv, sdwan_underlay_report.md
  overlay  : <stem>_sdwan_overlay.txt, ns_model_overlay.json,
             sdwan_overlay_report.md

The tool has no dependency on a live vManage -- all input comes from local
files.
"""
from __future__ import annotations

import argparse
import csv
import json
import pathlib
import sys
from typing import Any, Dict, List, Optional

# Windows consoles default to cp1252 and choke on non-ASCII; force UTF-8 so the
# progress output (and any non-ASCII device names) print cleanly everywhere.
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except (AttributeError, ValueError):
    pass

from .sdwan_export_reader import load_export
from .sdwan_physical_mapper import build_physical_model
from .sdwan_logical_mapper import build_logical_model
from .sdwan_stencil_mapper import to_csv_rows
from .ns_command_builder import build_command_script
from .ns_model import model_to_dict


def _load_config(path: Optional[pathlib.Path]) -> Dict[str, Any]:
    """Read sdwan_to_ns_config.json; only each key's 'value' is used."""
    if not path or not path.is_file():
        return {}
    try:
        with path.open(encoding="utf-8-sig") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[WARN] could not read config {path}: {exc}", file=sys.stderr)
        return {}
    out: Dict[str, Any] = {}
    for key, blob in raw.items():
        if isinstance(blob, dict) and "value" in blob:
            out[key] = blob["value"]
        else:
            out[key] = blob
    return out


def _write_csv(path: pathlib.Path, rows: List[List[str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(rows)


def _write_report(path: pathlib.Path, title: str, counts: Dict[str, int],
                  caveats: List[str]) -> None:
    lines = [f"# {title}\n"]
    lines.append("## Counts\n")
    for k, v in counts.items():
        lines.append(f"- **{k}**: {v}")
    lines.append("")
    if caveats:
        lines.append("## Accuracy caveats\n")
        for c in caveats:
            lines.append(f"- {c}")
        lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _run_underlay(idx, cfg, layout, out_dir, stem) -> None:
    """Underlay = the physical/circuit view (all devices + inferred VPN0
    transport-circuit links)."""
    print("[underlay] building physical underlay model ...")
    model, info = build_physical_model(idx, cfg, layout=layout)
    script = build_command_script(model)

    underlay_name = f"{stem}_sdwan_underlay.txt"
    (out_dir / underlay_name).write_text(script.text(), encoding="utf-8")
    (out_dir / "ns_model_underlay.json").write_text(
        json.dumps(model_to_dict(model), indent=2, ensure_ascii=False), encoding="utf-8")
    _write_csv(out_dir / "sdwan_inventory.csv", to_csv_rows(info["mappings"]))
    _write_report(
        out_dir / "sdwan_underlay_report.md",
        "SD-WAN (vManage) -> NS -- Underlay (physical/circuit) Report",
        info["counts"], info["caveats"],
    )
    c = info["counts"]
    print(f"  devices: vmanage={c['vmanage']} vsmart={c['vsmart']} vbond={c['vbond']} "
          f"edge={c['edge']}, transport_segments={c['transport_segments']} "
          f"(single-member={c['single_member_segments']}, "
          f"multi-subnet={c['multi_subnet_segments']}, "
          f"mesh-annotated={c['mesh_annotated']}), "
          f"lan_dummy={c['lan_dummy']}, "
          f"control_plane={c['control_dummy_devices']} device(s) "
          f"(uplinks={c['control_uplinks']}, "
          f"controllers={c['controllers_linked']}, "
          f"rows={c['control_conn_rows']}/{c['control_conn_unresolved']} unresolved), "
          f"l1_links={c['l1_links']} (lan_links={c['lan_links']}, "
          f"control_links={c['control_links']}), "
          f"l2_segments={c['l2_segments']} "
          f"(control={c['control_l2_segments']}), "
          f"cloud_svi_ports={c['cloud_svi_ports']}, "
          f"loopbacks={c['service_vpn_loopbacks']}, "
          f"ip_assignments={c['ip_assignments']}, "
          f"vrfs={c['vrfs']} (renames={c['vrf_renames']}: "
          f"transport={c['transport_vrf_renames']}, lan={c['lan_vrf_renames']}), "
          f"commands={sum(script.counts.values())}")
    print(f"  -> {underlay_name}, ns_model_underlay.json, sdwan_inventory.csv, "
          "sdwan_underlay_report.md")


def _run_overlay(idx, cfg, layout, out_dir, stem) -> None:
    """Overlay = the Service VPN (VRF/LAN) view (Edges with at least one
    Service VPN interface, one waypoint cloud per VPN above them and one
    synthetic LAN switch per Edge below them; no VPN0, no Tunnel, no
    transport colours, no OMP route state)."""
    print("[overlay] building SD-WAN Service VPN overlay model ...")
    model, info = build_logical_model(idx, cfg, layout=layout)
    # Overlay ports are logical ('Dummy' links on the cloud side) — mark
    # Speed/Duplex/Port Type as Unknown rather than inventing physical values.
    script = build_command_script(model, port_info_unknown=True)

    overlay_name = f"{stem}_sdwan_overlay.txt"
    (out_dir / overlay_name).write_text(script.text(), encoding="utf-8")
    (out_dir / "ns_model_overlay.json").write_text(
        json.dumps(model_to_dict(model), indent=2, ensure_ascii=False), encoding="utf-8")
    _write_report(
        out_dir / "sdwan_overlay_report.md",
        "SD-WAN (vManage) -> NS -- Overlay (Service VPN) Report",
        info["counts"], info["caveats"],
    )
    c = info["counts"]
    print(f"  edge={c['edge']} service_vpn_waypoints={c['service_vpn_waypoint']} "
          f"(unnamed={c['unresolved_vpn_name']}) lan_dummy={c['lan_dummy']}, "
          f"l1_links={c['l1_links']} (vpn_ports={c['vpn_ports']}, "
          f"lan_links={c['lan_links']}), "
          f"l2_segments={c['l2_segments']}, virtual_ports={c['virtual_ports']}, "
          f"cloud_svi_ports={c['cloud_svi_ports']}, "
          f"ip_assignments={c['ip_assignments']}, "
          f"commands={sum(script.counts.values())}")
    print(f"  -> {overlay_name}, ns_model_overlay.json, sdwan_overlay_report.md")


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Convert a Cisco Catalyst SD-WAN (vManage) export into Network Sketcher commands.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--input", "-i", required=True, metavar="EXPORT",
        help="vManage export: a single combined .json, or a directory of *.json.",
    )
    parser.add_argument(
        "--mode", "-m",
        choices=["underlay", "overlay", "both"], default="both",
        help="Which diagram(s) to generate: 'underlay' (physical/circuit view), "
             "'overlay' (Service VPN / VRF view), or 'both' (default).",
    )
    parser.add_argument(
        "--out", "-o", default="ns_commands.txt", metavar="OUTPUT_FILE",
        help="Base output path; mode-specific scripts and side-outputs are "
             "written to its directory. Default: ns_commands.txt",
    )
    parser.add_argument(
        "--config", "-c", default=None, metavar="CONFIG_JSON",
        help="Path to sdwan_to_ns_config.json (optional).",
    )
    parser.add_argument(
        "--layout", "-l", choices=["auto", "coordinate", "tier"], default="tier",
        help="Device placement strategy. vManage exports carry no canvas "
             "coordinates, so 'tier' (role-based hierarchy) is the default.",
    )
    args = parser.parse_args(argv)

    input_path = pathlib.Path(args.input).resolve()
    out_path = pathlib.Path(args.out).resolve()
    out_dir = out_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = out_path.stem or "ns_commands"
    cfg = _load_config(pathlib.Path(args.config).resolve() if args.config else None)

    print(f"[1/2] Loading vManage export: {input_path}")
    try:
        idx = load_export(input_path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    summary = idx.summary()
    print(f"        devices={summary['devices']} interfaces={summary['interfaces']} "
          f"wan_interfaces={summary['wan_interfaces']} "
          f"vpn_names={summary['vpn_names']}")

    print("[2/2] Generating Network Sketcher commands ...")
    failed = []
    if args.mode in ("underlay", "both"):
        try:
            _run_underlay(idx, cfg, args.layout, out_dir, stem)
        except Exception as exc:  # keep going so the other mode still emits
            failed.append("underlay")
            print(f"  [ERROR] underlay generation failed: {exc}", file=sys.stderr)
    if args.mode in ("overlay", "both"):
        try:
            _run_overlay(idx, cfg, args.layout, out_dir, stem)
        except Exception as exc:
            failed.append("overlay")
            print(f"  [ERROR] overlay generation failed: {exc}", file=sys.stderr)

    print("\n[Done] All outputs written to:", out_dir)
    if failed:
        print(f"[WARN] {', '.join(failed)} mode(s) failed; other outputs were still written.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
