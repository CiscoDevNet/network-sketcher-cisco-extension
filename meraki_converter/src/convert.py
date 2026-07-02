# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""convert.py — Meraki export JSON -> Network Sketcher command converter.

Reads the JSON produced by ``fetch_from_meraki.py`` (or an equivalent saved
snapshot) and emits a ready-to-use Network Sketcher command script plus audit
side-outputs. No live API access — all input comes from the local file, so the
conversion is reproducible after the Sandbox reservation expires.

Usage (run as a module from the repository root)::

    python -m meraki_converter.src.convert \\
        --input  meraki_converter/Input_data/meraki_export.json \\
        [--out   meraki_converter/Output_data/ns_commands_meraki.txt] \\
        [--config meraki_converter/meraki_to_ns_config.json] \\
        [--layout tier]

Outputs (written to the same directory as ``--out``):

    <stem>.txt            the NS command script (main deliverable)
    ns_model_meraki.json  the intermediate NSModel (debug)
    meraki_inventory.csv  device -> stencil audit
    meraki_report.md      counts + accuracy caveats
"""
from __future__ import annotations

import argparse
import csv
import json
import pathlib
import sys
from typing import Any, Dict, List, Optional

# Windows consoles default to cp1252 and choke on non-ASCII; force UTF-8.
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except (AttributeError, ValueError):
    pass

from .meraki_reader import load_export
from .meraki_stencil_mapper import to_csv_rows
from .meraki_topology_mapper import build_model
from .ns_command_builder import build_command_script
from .ns_model import model_to_dict


def _load_config(path: Optional[pathlib.Path]) -> Dict[str, Any]:
    """Read meraki_to_ns_config.json; only each key's 'value' is used."""
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
    lines = [f"# {title}\n", "## Counts\n"]
    for k, v in counts.items():
        lines.append(f"- **{k}**: {v}")
    lines.append("")
    if caveats:
        lines.append("## Accuracy caveats\n")
        for c in caveats:
            lines.append(f"- {c}")
        lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Convert a Meraki Dashboard export into Network Sketcher commands.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--input", "-i", required=True, metavar="EXPORT",
                        help="Meraki export JSON (from fetch_from_meraki.py).")
    parser.add_argument("--out", "-o", default="ns_commands_meraki.txt", metavar="OUTPUT_FILE",
                        help="Base output path; side-outputs go to its directory. "
                             "Default: ns_commands_meraki.txt")
    parser.add_argument("--config", "-c", default=None, metavar="CONFIG_JSON",
                        help="Path to meraki_to_ns_config.json (optional).")
    parser.add_argument("--layout", "-l", choices=["auto", "coordinate", "tier"], default="tier",
                        help="Device placement strategy. Meraki carries no canvas "
                             "coordinates, so 'tier' (role hierarchy) is the default.")
    args = parser.parse_args(argv)

    input_path = pathlib.Path(args.input).resolve()
    out_path = pathlib.Path(args.out).resolve()
    out_dir = out_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = out_path.stem or "ns_commands_meraki"
    cfg = _load_config(pathlib.Path(args.config).resolve() if args.config else None)

    print(f"[1/2] Loading Meraki export: {input_path}")
    try:
        export = load_export(str(input_path))
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    print(f"        networks={len(export.networks)} devices={len(export.devices)} "
          f"switchPorts={len(export.switch_ports)}")

    print("[2/2] Generating Network Sketcher commands ...")
    model, info = build_model(export, cfg, layout=args.layout)
    script = build_command_script(model)

    (out_dir / f"{stem}.txt").write_text(script.text(), encoding="utf-8")
    (out_dir / "ns_model_meraki.json").write_text(
        json.dumps(model_to_dict(model), indent=2, ensure_ascii=False), encoding="utf-8")
    _write_csv(out_dir / "meraki_inventory.csv", to_csv_rows(info["mappings"]))
    _write_report(out_dir / "meraki_report.md",
                  "Meraki -> NS Conversion Report", info["counts"], info["caveats"])

    c = info["counts"]
    print(f"  devices={c['devices']} (+{c['internet_waypoints']} internet wp) "
          f"l1_links={c['l1_links']} l2_segments={c['l2_segments']} "
          f"ip={c['ip_assignments']} clients={c['clients']}, "
          f"commands={sum(script.counts.values())}")
    print(f"  -> {stem}.txt, ns_model_meraki.json, meraki_inventory.csv, meraki_report.md")
    print("\n[Done] All outputs written to:", out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
