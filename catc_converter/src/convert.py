# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""convert.py — Cisco Catalyst Center (DNA Center) export -> Network Sketcher converter.

Reads a Catalyst Center export (the combined JSON written by
``fetch_from_catc.py``, a directory of per-endpoint ``*.json`` dumps, or a
``.tar.gz`` of either) and emits ready-to-use Network Sketcher command scripts in
two selectable modes:

  * ``underlay`` — the physical campus: core / distribution / access switches,
    routers, WLCs and APs plus their REAL observed L1 links (from Catalyst
    Center ``physicalTopology``; no inference). Unmanaged neighbours become gray
    waypoints.
  * ``overlay``  — the logical SD-Access fabric: per-VN gateways + anycast-gateway
    segments (Vlan + SVI), border hand-off clouds, and optional clients. Every
    overlay device is a logical construct and is coloured light purple.

Usage (run as a module from the repository root)::

    python -m catc_converter.src.convert \\
        --input  catc_export.json \\
        [--mode  underlay|overlay|both]  \\
        [--out   ns_commands.txt]        \\
        [--config catc_to_ns_config.json] \\
        [--layout tier]

Outputs (written to the same directory as ``--out``):

  underlay : <stem>_catc_underlay(L1Only).txt, ns_model_underlay.json,
             catc_inventory.csv, catc_underlay_report.md
  overlay  : <stem>_catc_overlay.txt, ns_model_overlay.json,
             gen_flow_list.csv, catc_overlay_report.md

The tool has no dependency on a live Catalyst Center — all input comes from
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

from .catc_export_reader import load_export
from .catc_physical_mapper import build_physical_model
from .catc_logical_mapper import build_logical_model
from .catc_stencil_mapper import to_csv_rows
from .flow_list_builder import build_flow_rows, write_flow_list_csv
from .ns_command_builder import build_command_script
from .ns_model import model_to_dict


def _load_config(path: Optional[pathlib.Path]) -> Dict[str, Any]:
    """Read catc_to_ns_config.json; only each key's 'value' is used."""
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
    """Underlay = the physical campus (core / distribution / access + real L1)."""
    print("[underlay] building physical campus model ...")
    model, info = build_physical_model(idx, cfg, layout=layout)
    script = build_command_script(model)

    underlay_name = f"{stem}_catc_underlay(L1Only).txt"
    (out_dir / underlay_name).write_text(script.text(), encoding="utf-8")
    (out_dir / "ns_model_underlay.json").write_text(
        json.dumps(model_to_dict(model), indent=2, ensure_ascii=False), encoding="utf-8")
    _write_csv(out_dir / "catc_inventory.csv", to_csv_rows(info["mappings"]))
    _write_report(
        out_dir / "catc_underlay_report.md",
        "Catalyst Center -> NS — Underlay (physical campus) Report",
        info["counts"], info["caveats"],
    )
    c = info["counts"]
    print(f"  nodes: core={c['core']} dist={c['distribution']} access={c['access']} "
          f"border={c['border']} router={c['router']} wlc={c['wlc']} ap={c['ap']} "
          f"external={c['external']}, "
          f"l1_links={c['l1_links']} (inferred={c.get('inferred_links', 0)}, "
          f"inferred_segments={c.get('inferred_segments', 0)}), "
          f"commands={sum(script.counts.values())}")
    print(f"  -> {underlay_name}, ns_model_underlay.json, catc_inventory.csv, catc_underlay_report.md")


def _run_overlay(idx, cfg, layout, out_dir, stem) -> None:
    """Overlay = the logical SD-Access fabric (VN / anycast gateway)."""
    print("[overlay] building logical overlay model ...")
    model, vn_name_by_key, info = build_logical_model(idx, cfg, layout=layout)
    script = build_command_script(model, port_info_unknown=True)
    flow_rows = build_flow_rows(idx, vn_name_by_key, cfg)

    overlay_name = f"{stem}_catc_overlay.txt"
    (out_dir / overlay_name).write_text(script.text(), encoding="utf-8")
    (out_dir / "ns_model_overlay.json").write_text(
        json.dumps(model_to_dict(model), indent=2, ensure_ascii=False), encoding="utf-8")
    write_flow_list_csv(str(out_dir / "gen_flow_list.csv"), flow_rows)
    _write_report(
        out_dir / "catc_overlay_report.md",
        "Catalyst Center -> NS — Overlay (logical SD-Access) Report",
        info["counts"], info["caveats"],
        extra=["## Flows\n", f"- **SGT flow rows**: {len(flow_rows)}"],
    )
    c = info["counts"]
    print(f"  fabric_sites={c['fabric_site']} fabric_collapsed={c.get('fabric_collapsed', 0)} "
          f"vn={c['vn']} vn_devices={c.get('vn_device', 0)} anycast_gw={c['anycast_gw']} "
          f"segment={c['segment']} border_cloud={c.get('border_cloud', 0)} "
          f"control_plane={c.get('control_plane', 0)} border={c.get('border', 0)} "
          f"edge={c.get('edge', 0)} edge_svi={c.get('edge_svi', 0)} host={c.get('host', 0)}, "
          f"flow_rows={len(flow_rows)}, commands={sum(script.counts.values())}")
    print(f"  -> {overlay_name}, ns_model_overlay.json, gen_flow_list.csv, catc_overlay_report.md")


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Convert a Cisco Catalyst Center / DNA Center export into Network Sketcher commands.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--input", "-i", required=True, metavar="EXPORT",
        help="Catalyst Center export: the combined JSON from fetch_from_catc, a "
             "directory of *.json, or a .tar.gz / .tgz of either.",
    )
    parser.add_argument(
        "--mode", "-m",
        choices=["underlay", "overlay", "both"], default="both",
        help="Which diagram(s) to generate: 'underlay' (physical campus), "
             "'overlay' (logical SD-Access), or 'both' (default).",
    )
    parser.add_argument(
        "--out", "-o", default="ns_commands.txt", metavar="OUTPUT_FILE",
        help="Base output path; mode-specific scripts and side-outputs are "
             "written to its directory. Default: ns_commands.txt",
    )
    parser.add_argument(
        "--config", "-c", default=None, metavar="CONFIG_JSON",
        help="Path to catc_to_ns_config.json (optional).",
    )
    parser.add_argument(
        "--layout", "-l", choices=["auto", "coordinate", "tier"], default="tier",
        help="Device placement strategy. Catalyst Center exports carry no canvas "
             "coordinates, so 'tier' (role-based hierarchy) is the default.",
    )
    args = parser.parse_args(argv)

    input_path = pathlib.Path(args.input).resolve()
    out_path = pathlib.Path(args.out).resolve()
    out_dir = out_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = out_path.stem or "ns_commands"
    cfg = _load_config(pathlib.Path(args.config).resolve() if args.config else None)

    print(f"[1/2] Loading Catalyst Center export: {input_path}")
    try:
        idx = load_export(input_path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    summary = idx.summary()
    print(f"        devices={summary['devices']} links={summary['links']} "
          f"fabricSites={summary['fabricSites']} vns={summary['vns']} "
          f"anycastGateways={summary['anycastGateways']} clients={summary['clients']} "
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
