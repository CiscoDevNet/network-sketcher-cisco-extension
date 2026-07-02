# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""convert.py — Nexus Dashboard (NDFC) export -> Network Sketcher command converter.

Reads a Nexus Dashboard / Fabric-Controller export (the combined JSON written by
``fetch_from_nd.py``, a directory of per-endpoint ``*.json`` dumps, or a
``.tar.gz`` of either) and emits ready-to-use Network Sketcher command scripts in
two selectable modes:

  * ``underlay`` — the physical fabric: leaf / spine / border / border-gateway
    switches plus their REAL observed L1 links (from NDFC ``control/links``;
    no CLOS inference). External neighbours become gray waypoints.
  * ``overlay``  — the logical VXLAN EVPN policy: VRF gateways + Network (L2VNI)
    segments with their anycast-gateway SVIs. Every overlay device is a logical
    construct and is coloured light purple.

Usage (run as a module from the repository root)::

    python -m nd_converter.src.convert \\
        --input  nd_export.json \\
        [--mode  underlay|overlay|both]  \\
        [--out   ns_commands.txt]        \\
        [--config nd_to_ns_config.json]  \\
        [--layout tier]

Outputs (written to the same directory as ``--out``):

  underlay : <stem>_nd_underlay(L1Only).txt, ns_model_underlay.json,
             nd_inventory.csv, nd_underlay_report.md
  overlay  : <stem>_nd_overlay.txt, ns_model_overlay.json,
             gen_flow_list.csv, nd_overlay_report.md

The tool has no dependency on a live Nexus Dashboard — all input comes from
local files.
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

from .nd_export_reader import load_export
from .nd_physical_mapper import build_physical_model
from .nd_logical_mapper import build_logical_model
from .nd_stencil_mapper import to_csv_rows
from .flow_list_builder import build_flow_rows, write_flow_list_csv
from .ns_command_builder import build_command_script
from .ns_model import model_to_dict


def _load_config(path: Optional[pathlib.Path]) -> Dict[str, Any]:
    """Read nd_to_ns_config.json; only each key's 'value' is used."""
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
                  caveats: List[str], extra: Optional[List[str]] = None) -> None:
    lines = [f"# {title}\n"]
    lines.append("## Counts\n")
    for k, v in counts.items():
        lines.append(f"- **{k}**: {v}")
    lines.append("")
    if extra:
        lines.extend(extra)
        lines.append("")
    if caveats:
        lines.append("## Accuracy caveats\n")
        for c in caveats:
            lines.append(f"- {c}")
        lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _run_underlay(idx, cfg, layout, out_dir, stem) -> None:
    """Underlay = the physical fabric (leaf / spine / border / BGW + real L1)."""
    print("[underlay] building physical fabric model ...")
    model, info = build_physical_model(idx, cfg, layout=layout)
    script = build_command_script(model)

    underlay_name = f"{stem}_nd_underlay(L1Only).txt"
    (out_dir / underlay_name).write_text(script.text(), encoding="utf-8")
    (out_dir / "ns_model_underlay.json").write_text(
        json.dumps(model_to_dict(model), indent=2, ensure_ascii=False), encoding="utf-8")
    _write_csv(out_dir / "nd_inventory.csv", to_csv_rows(info["mappings"]))
    _write_report(
        out_dir / "nd_underlay_report.md",
        "Nexus Dashboard -> NS — Underlay (physical fabric) Report",
        info["counts"], info["caveats"],
    )
    c = info["counts"]
    print(f"  nodes: spine={c['spine']} leaf={c['leaf']} border={c['border']} "
          f"bgw={c['bgw']} router={c['router']} external={c['external']} "
          f"(vpc={c.get('vpc_switches', 0)}), "
          f"l1_links={c['l1_links']}, commands={sum(script.counts.values())}")
    print(f"  -> {underlay_name}, ns_model_underlay.json, nd_inventory.csv, nd_underlay_report.md")


def _run_overlay(idx, cfg, layout, out_dir, stem) -> None:
    """Overlay = the logical VXLAN EVPN policy (VRF / Network)."""
    print("[overlay] building logical overlay model ...")
    model, net_name_by_key, info = build_logical_model(idx, cfg, layout=layout)
    script = build_command_script(model, port_info_unknown=True)
    flow_rows = build_flow_rows(idx, net_name_by_key, cfg)

    overlay_name = f"{stem}_nd_overlay.txt"
    (out_dir / overlay_name).write_text(script.text(), encoding="utf-8")
    (out_dir / "ns_model_overlay.json").write_text(
        json.dumps(model_to_dict(model), indent=2, ensure_ascii=False), encoding="utf-8")
    write_flow_list_csv(str(out_dir / "gen_flow_list.csv"), flow_rows)
    _write_report(
        out_dir / "nd_overlay_report.md",
        "Nexus Dashboard -> NS — Overlay (logical VXLAN EVPN) Report",
        info["counts"], info["caveats"],
        extra=["## Flows\n", f"- **intra-VRF flow rows**: {len(flow_rows)}"],
    )
    c = info["counts"]
    print(f"  fabrics={c['fabric']} vrf={c['vrf']} network={c['network']} "
          f"subnet={c['subnet']} host={c.get('host', 0)} (l2_only={c['l2_only']}), "
          f"flow_rows={len(flow_rows)}, commands={sum(script.counts.values())}")
    print(f"  -> {overlay_name}, ns_model_overlay.json, gen_flow_list.csv, nd_overlay_report.md")


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Convert a Nexus Dashboard / NDFC export into Network Sketcher commands.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--input", "-i", required=True, metavar="EXPORT",
        help="ND export: the combined JSON from fetch_from_nd, a directory of "
             "*.json, or a .tar.gz / .tgz of either.",
    )
    parser.add_argument(
        "--mode", "-m",
        choices=["underlay", "overlay", "both"], default="both",
        help="Which diagram(s) to generate: 'underlay' (physical fabric), "
             "'overlay' (logical VXLAN EVPN), or 'both' (default).",
    )
    parser.add_argument(
        "--out", "-o", default="ns_commands.txt", metavar="OUTPUT_FILE",
        help="Base output path; mode-specific scripts and side-outputs are "
             "written to its directory. Default: ns_commands.txt",
    )
    parser.add_argument(
        "--config", "-c", default=None, metavar="CONFIG_JSON",
        help="Path to nd_to_ns_config.json (optional).",
    )
    parser.add_argument(
        "--layout", "-l", choices=["auto", "coordinate", "tier"], default="tier",
        help="Device placement strategy. NDFC exports carry no canvas "
             "coordinates, so 'tier' (role-based hierarchy) is the default.",
    )
    args = parser.parse_args(argv)

    input_path = pathlib.Path(args.input).resolve()
    out_path = pathlib.Path(args.out).resolve()
    out_dir = out_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = out_path.stem or "ns_commands"
    cfg = _load_config(pathlib.Path(args.config).resolve() if args.config else None)

    print(f"[1/2] Loading Nexus Dashboard export: {input_path}")
    try:
        idx = load_export(input_path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    summary = idx.summary()
    print(f"        fabrics={summary['fabrics']} switches={summary['switches']} "
          f"links={summary['links']} vrfs={summary['vrfs']} networks={summary['networks']} "
          f"endpoints={summary['endpoints']} vpcPairs={summary['vpcPairs']} "
          f"interfaces={summary['interfaces']}")

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
