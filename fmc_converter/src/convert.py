# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""Convert a Firewall Management Center export into Network Sketcher commands.

The export is a local JSON file written by ``fetch_from_fmc.py``. Conversion
does not contact FMC.

    python -m fmc_converter.src.convert \\
        -i fmc_converter/Input_data/fmc_export.json \\
        -o fmc_converter/Output_data/ns_commands_fmc.txt
"""
from __future__ import annotations

import argparse
import csv
import json
import pathlib
import sys
from typing import Any, Dict, List, Optional

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except (AttributeError, ValueError):
    pass

from .fmc_mapper import MapperReport, build_model
from .fmc_reader import load_export
from .fmc_stencil_mapper import to_csv_rows
from .ns_command_builder import build_command_script
from .ns_model import model_to_dict

_DEFAULT_CONFIG = pathlib.Path(__file__).resolve().parent.parent / "fmc_to_ns_config.json"


def _load_config(path: Optional[pathlib.Path]) -> Dict[str, Any]:
    """Read fmc_to_ns_config.json; only each key's ``value`` is used."""
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


def _write_report(path: pathlib.Path, report: MapperReport, n_commands: int) -> None:
    lines = [
        "# Secure Firewall Management Center -> Network Sketcher — Conversion Report\n",
        "## Counts\n",
        f"- **devices**: {report.devices}",
        f"- **l1_links**: {report.l1_links}",
        f"- **NS commands emitted**: {n_commands}",
        "",
        "## Interface direction\n",
        "Management and outside interfaces sit above the firewall. DMZ interfaces",
        "sit to its right. Inside interfaces, and any interface whose name matches",
        "no direction keyword, sit along the bottom. Each of those is a grey",
        "switch that does not exist on FMC. The firewall's management switch",
        "reaches FMC through the `Dummy_wan` waypoint, not by a direct cable.",
        "A firewall default route is placed on the grey switch that faces the egress",
        "interface, using that interface's prefix. FMC's own address and default",
        "gateway are placed on FMC and on Dummy_fmc Ethernet 0 when the export",
        "contains them. The REST API does not return the FMC appliance address.",
        "",
    ]
    if report.placements:
        for line in report.placements:
            lines.append(f"- {line}")
    else:
        lines.append("- (none)")
    lines.extend(["", "## Skipped interfaces\n"])
    if report.skipped:
        for line in report.skipped:
            lines.append(f"- {line}")
    else:
        lines.append("- (none)")
    lines.extend(["", "## Notes\n"])
    notes = report.notes or ["Grey switches and `Dummy_wan` are synthesised."]
    for note in notes:
        lines.append(f"- {note}")
    if report.notes:
        lines.append("- Grey switches and `Dummy_wan` are synthesised.")
    lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Convert an FMC export into a Network Sketcher command script.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--input", "-i", required=True, metavar="EXPORT",
                        help="FMC export JSON (from fetch_from_fmc.py).")
    parser.add_argument("--out", "-o", default="ns_commands_fmc.txt", metavar="OUTPUT_FILE",
                        help="Command-script path. Other files are written beside it. "
                             "Default: ns_commands_fmc.txt")
    parser.add_argument("--config", "-c", default=str(_DEFAULT_CONFIG), metavar="CONFIG_JSON",
                        help="Path to fmc_to_ns_config.json.")
    args = parser.parse_args(argv)

    input_path = pathlib.Path(args.input).resolve()
    out_path = pathlib.Path(args.out).resolve()
    out_dir = out_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = out_path.stem or "ns_commands_fmc"
    cfg = _load_config(pathlib.Path(args.config).resolve() if args.config else None)

    print(f"[1/2] Loading FMC export: {input_path}")
    try:
        data = load_export(input_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    print(f"        fmc={data.fmc_name} devices={len(data.devices)} ha_pairs={len(data.ha_pairs)}")

    print("[2/2] Building NSModel and generating commands ...")
    model, _devices, report = build_model(data, cfg)
    script = build_command_script(model)
    n_commands = sum(script.counts.values())

    (out_dir / f"{stem}.txt").write_text(script.text(), encoding="utf-8")
    (out_dir / "ns_model_fmc.json").write_text(
        json.dumps(model_to_dict(model), indent=2, ensure_ascii=False), encoding="utf-8")
    _write_csv(out_dir / "fmc_inventory.csv", to_csv_rows(model.devices))
    _write_report(out_dir / "fmc_report.md", report, n_commands)

    print(f"  devices={report.devices} l1_links={report.l1_links} commands={n_commands}")
    for line in report.placements:
        print(f"  [dir] {line}")
    for note in report.notes:
        print(f"  [note] {note}")
    print(f"  -> {stem}.txt, ns_model_fmc.json, fmc_inventory.csv, fmc_report.md")
    print("\n[Done] All outputs written to:", out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
