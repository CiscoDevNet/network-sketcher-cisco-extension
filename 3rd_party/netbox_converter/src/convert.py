# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""convert.py — NetBox export -> Network Sketcher command converter.

Reads the combined JSON written by ``fetch_from_netbox.py`` (or a hand-saved API
dump) and emits a ready-to-run Network Sketcher command script covering L1
(cabled links, patch-panel pass-through resolved), L2 (access/trunk VLANs, SVIs)
and L3 (interface IPs + VRFs).

Real devices are **role-coloured** by the shared palette (green network gear /
red server / yellow client), identical to the sna / cv / cml / aci / nd
converters. Observed WAN / provider **WayPoints** (a real NetBox
``circuits.providernetwork`` such as "Level3 MPLS") get NS's native **light
blue** ``(220,230,242)``. Only synthesised ``dummy_stub_*`` placeholders
(stand-ins for uncabled VLAN/IP ports) are forced **light gray** ``(200,200,200)``
to flag them as inferred rather than observed.

Usage (run as a module; from the ``3rd_party/`` directory)::

    python -m netbox_converter.src.convert \\
        --input  netbox_converter/Input_data/netbox_export.json \\
        [--out    netbox_converter/Output_data/ns_commands.txt] \\
        [--config netbox_converter/netbox_to_ns_config.json]

Outputs (written to the same directory as ``--out``):

    <stem>_netbox.txt        the NS command script
    ns_model_netbox.json     the intermediate NSModel (debug)
    netbox_inventory.csv     device -> stencil mapping (audit)
    netbox_report.md         counts + accuracy caveats

No live NetBox connection is needed at conversion time — all input is local.
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

from .netbox_reader import load_export
from .netbox_mapper import build_model
from .netbox_stencil_mapper import to_csv_rows
from .ns_command_builder import build_command_script
from .ns_model import model_to_dict


def _load_config(path: Optional[pathlib.Path]) -> Dict[str, Any]:
    """Read netbox_to_ns_config.json; only each key's 'value' is used."""
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


def _write_report(path: pathlib.Path, meta: Dict[str, Any], report, n_commands: int) -> None:
    lines = [
        "# NetBox -> Network Sketcher — Conversion Report\n",
        f"Source: {meta.get('url', '?')} (NetBox {meta.get('netbox_version', '?')})\n",
        "## Counts\n",
        f"- **devices**: {report.devices}",
        f"- **l1_links**: {report.l1_links}",
        f"- **ip_assignments**: {report.ip_assignments}",
        f"- **port_channels**: {report.port_channels}",
        f"- **svis**: {report.svis}",
        f"- **l2 access / trunk**: {report.l2_access} / {report.l2_trunk}",
        f"- **vrf_renames**: {report.vrf_renames}",
        f"- **NS commands emitted**: {n_commands}",
        "",
        "## Notes / accuracy caveats\n",
        "- All devices are coloured light gray (200,200,200): NetBox is a "
        "database and may be out of sync with the live network — verify before use.",
    ]
    for note in report.notes:
        lines.append(f"- {note}")
    lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Convert a NetBox export into a Network Sketcher command script.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--input", "-i", required=True, metavar="EXPORT",
                        help="NetBox export JSON (from fetch_from_netbox.py).")
    parser.add_argument("--out", "-o", default="ns_commands.txt", metavar="OUTPUT_FILE",
                        help="Base output path; side-outputs go to its directory. "
                             "Default: ns_commands.txt")
    parser.add_argument("--config", "-c", default=None, metavar="CONFIG_JSON",
                        help="Path to netbox_to_ns_config.json (optional).")
    args = parser.parse_args(argv)

    input_path = pathlib.Path(args.input).resolve()
    out_path = pathlib.Path(args.out).resolve()
    out_dir = out_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = out_path.stem or "ns_commands"
    cfg = _load_config(pathlib.Path(args.config).resolve() if args.config else None)

    print(f"[1/2] Loading NetBox export: {input_path}")
    try:
        data = load_export(str(input_path))
    except (FileNotFoundError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    print(f"        devices={len(data.devices)} interfaces={len(data.interfaces)} "
          f"cables={len(data.cables)} ip_addresses={len(data.ip_addresses)} "
          f"vlans={len(data.vlans)} vrfs={len(data.vrfs)}")

    print("[2/2] Building NSModel and generating commands ...")
    model, mappings, report = build_model(data, cfg)
    script = build_command_script(model)
    n_commands = sum(script.counts.values())

    script_name = f"{stem}_netbox.txt"
    (out_dir / script_name).write_text(script.text(), encoding="utf-8")
    (out_dir / "ns_model_netbox.json").write_text(
        json.dumps(model_to_dict(model), indent=2, ensure_ascii=False), encoding="utf-8")
    _write_csv(out_dir / "netbox_inventory.csv", to_csv_rows(mappings))
    _write_report(out_dir / "netbox_report.md", data.meta, report, n_commands)

    print(f"  devices={report.devices} l1_links={report.l1_links} "
          f"ip={report.ip_assignments} pc={report.port_channels} svi={report.svis} "
          f"l2(access/trunk)={report.l2_access}/{report.l2_trunk} vrf={report.vrf_renames}")
    print(f"  commands={n_commands}")
    for note in report.notes:
        print(f"  [note] {note}")
    print(f"  -> {script_name}, ns_model_netbox.json, netbox_inventory.csv, netbox_report.md")
    print("\n[Done] All outputs written to:", out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
