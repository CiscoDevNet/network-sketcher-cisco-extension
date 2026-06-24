# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""convert.py — APIC Configuration Export -> Network Sketcher command converter.

Reads an APIC Configuration Export (a ``.tar.gz`` backup, a directory of
extracted ``*.json`` files, or a single merged JSON) and emits ready-to-use
Network Sketcher command scripts in two selectable modes:

  * ``underlay`` — the physical fabric: spine / leaf / border-leaf / APIC nodes
    plus their L1 links (observed via LLDP when available, else inferred CLOS).
  * ``overlay``  — the logical policy: Tenant / VRF / BD / EPG hierarchy + real
    endpoints, with contracts emitted as a ``[Flow_List]`` sheet. Every overlay
    device is a logical construct and is coloured light purple.

Usage (run as a module from the repository root)::

    python -m aci_converter.src.convert \\
        --input  config_export.tar.gz \\
        [--mode  underlay|overlay|both]  \\
        [--out   ns_commands.txt]        \\
        [--config aci_to_ns_config.json] \\
        [--layout tier]

Outputs (written to the same directory as ``--out``):

  underlay : <stem>_aci_underlay(L1Only).txt, ns_model_underlay.json,
             aci_inventory.csv, aci_underlay_report.md
  overlay  : <stem>_aci_overlay.txt, ns_model_overlay.json,
             gen_flow_list.csv, aci_overlay_report.md

The tool has no dependency on a live APIC — all input comes from local files.
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

from .aci_export_reader import load_mit
from .aci_physical_mapper import build_physical_model
from .aci_logical_mapper import build_logical_model
from .aci_stencil_mapper import to_csv_rows
from .flow_list_builder import build_flow_rows, write_flow_list_csv
from .ns_command_builder import build_command_script
from .ns_model import model_to_dict


def _load_config(path: Optional[pathlib.Path]) -> Dict[str, Any]:
    """Read aci_to_ns_config.json; only each key's 'value' is used."""
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
    """Underlay = the physical fabric (spine / leaf / border-leaf / APIC + L1)."""
    print("[underlay] building physical fabric model ...")
    model, info = build_physical_model(idx, cfg, layout=layout)
    script = build_command_script(model)

    underlay_name = f"{stem}_aci_underlay(L1Only).txt"
    (out_dir / underlay_name).write_text(script.text(), encoding="utf-8")
    (out_dir / "ns_model_underlay.json").write_text(
        json.dumps(model_to_dict(model), indent=2, ensure_ascii=False), encoding="utf-8")
    _write_csv(out_dir / "aci_inventory.csv", to_csv_rows(info["mappings"]))
    _write_report(
        out_dir / "aci_underlay_report.md",
        "ACI -> NS — Underlay (physical fabric) Report", info["counts"], info["caveats"],
    )
    c = info["counts"]
    print(f"  nodes: spine={c['spine']} leaf={c['leaf']} "
          f"(border={c['border_leaf']}) apic={c['controller']}, "
          f"l1_links={c['l1_links']}, commands={sum(script.counts.values())}")
    print(f"  source: nodes={info.get('node_source')} links={info.get('link_source')}")
    print(f"  -> {underlay_name}, ns_model_underlay.json, aci_inventory.csv, aci_underlay_report.md")


def _run_overlay(idx, cfg, layout, out_dir, stem) -> None:
    """Overlay = the logical policy (Tenant / VRF / BD / EPG + contracts).

    Every overlay device is a logical construct, so they are all coloured light
    purple (set by the overlay mapper)."""
    print("[overlay] building logical policy model ...")
    model, epg_name_by_dn, info = build_logical_model(idx, cfg, layout=layout)
    # Overlay ports are logical ('Dummy' links) — mark Speed/Duplex/Port Type
    # as Unknown rather than inventing physical values.
    script = build_command_script(model, port_info_unknown=True)
    flow_rows = build_flow_rows(idx, epg_name_by_dn, cfg)

    overlay_name = f"{stem}_aci_overlay.txt"
    (out_dir / overlay_name).write_text(script.text(), encoding="utf-8")
    (out_dir / "ns_model_overlay.json").write_text(
        json.dumps(model_to_dict(model), indent=2, ensure_ascii=False), encoding="utf-8")
    write_flow_list_csv(str(out_dir / "gen_flow_list.csv"), flow_rows)
    _write_report(
        out_dir / "aci_overlay_report.md",
        "ACI -> NS — Overlay (logical policy) Report", info["counts"], info["caveats"],
        extra=[f"## Flows\n", f"- **contract flow rows**: {len(flow_rows)}"],
    )
    c = info["counts"]
    print(f"  tenants={c['tenant']} vrf={c['vrf']} bd={c['bd']} epg={c['epg']} "
          f"endpoint={c.get('endpoint', 0)} (srv={c.get('srv', 0)}, "
          f"pc_seg={c.get('pc_segment', 0)}) l3out={c['l3out']}, "
          f"flow_rows={len(flow_rows)}, commands={sum(script.counts.values())}")
    print(f"  -> {overlay_name}, ns_model_overlay.json, gen_flow_list.csv, aci_overlay_report.md")


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Convert an APIC Configuration Export into Network Sketcher commands.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--input", "-i", required=True, metavar="EXPORT",
        help="APIC config export: a .tar.gz / .tgz, a directory of *.json, or a single .json.",
    )
    parser.add_argument(
        "--mode", "-m",
        choices=["underlay", "overlay", "both"], default="both",
        help="Which diagram(s) to generate: 'underlay' (physical fabric), "
             "'overlay' (logical policy), or 'both' (default).",
    )
    parser.add_argument(
        "--out", "-o", default="ns_commands.txt", metavar="OUTPUT_FILE",
        help="Base output path; mode-specific scripts and side-outputs are "
             "written to its directory. Default: ns_commands.txt",
    )
    parser.add_argument(
        "--config", "-c", default=None, metavar="CONFIG_JSON",
        help="Path to aci_to_ns_config.json (optional).",
    )
    parser.add_argument(
        "--layout", "-l", choices=["auto", "coordinate", "tier"], default="tier",
        help="Device placement strategy. ACI exports carry no canvas coordinates, "
             "so 'tier' (role-based hierarchy) is the default.",
    )
    args = parser.parse_args(argv)

    input_path = pathlib.Path(args.input).resolve()
    out_path = pathlib.Path(args.out).resolve()
    out_dir = out_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = out_path.stem or "ns_commands"
    cfg = _load_config(pathlib.Path(args.config).resolve() if args.config else None)

    print(f"[1/2] Loading APIC export: {input_path}")
    try:
        idx = load_mit(input_path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    summary = idx.summary()
    print(f"        {sum(summary.values())} MOs across {len(summary)} classes "
          f"(tenants={len(idx.of('fvTenant'))}, nodes={len(idx.of('fabricNodeIdentP'))}, "
          f"contracts={len(idx.of('vzBrCP'))})")

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
