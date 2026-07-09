# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""convert.py — a directory of Cisco running-config text files -> Network
Sketcher command converter.

STUB FILE — see ``config_converter/DESIGN.md`` sections 3 and 4.1/4.1.1 for
the full design. Unlike every other converter in this repo (which reads ONE
combined export file), config_converter's ``--input`` is a DIRECTORY
containing running-config text files (DESIGN.md section 4.1 — this is a
deliberate, documented deviation from ``template_converter/GUIDE.md``'s
single-file assumption). Confirmed design (DESIGN.md section 4.1.1, decision
12): a file may contain exactly ONE device's config, MULTIPLE devices'
configs concatenated together, or the input directory may contain a MIX of
both — ``_split_multi_device_blob()`` below handles all three
transparently, and the OS platform of each resulting chunk is auto-detected
independently (no manual OS selection option is offered).

Usage (once implemented; run as a module from the repository root)::

    python -m config_converter.src.convert \\
        --input  config_converter/Input_data/sample1/ \\
        [--out    config_converter/Output_data/ns_commands.txt] \\
        [--config config_converter/config_converter_to_ns_config.json]

Outputs (written to the same directory as ``--out``, TODO once build_model /
build_command_script are implemented):

    <stem>_config.txt          the NS command script (main deliverable)
    ns_model_config.json       the intermediate NSModel (debug)
    config_inventory.csv       device -> stencil mapping (audit)
    config_report.md           counts + accuracy caveats + ambiguous-match /
                                inferred-peer / WAN / closed-environment audit
                                trail (DESIGN.md sections 4.3-4.8 all require
                                this transparency — do not omit it)
    config_excluded_links.csv  candidate links excluded due to an
                                unresolvable cross-site RFC1918 subnet
                                collision (DESIGN.md section 4.3.9, decision
                                5) — modelled on sna_converter's
                                out_of_scope_ips.csv

No live device connection is needed at conversion time — all input is local
running-config text files.
"""
from __future__ import annotations

import argparse
import csv
import json
import pathlib
import re
import sys
from typing import Any, Dict, List, Optional, Set, Tuple

# Windows consoles default to cp1252 and choke on non-ASCII; force UTF-8 so
# progress output (and any non-ASCII device names) print cleanly everywhere.
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except (AttributeError, ValueError):
    pass

from .config_parser import parse_all
from .topology_mapper import build_model, write_excluded_links_csv
from .stencil_mapper import to_csv_rows
from .ns_command_builder import build_command_script
from .ns_model import model_to_dict

# Confirmed design (DESIGN.md section 4.1.1, decision 12): NO extension
# whitelist. Every file under Input_data (recursively, if cfg['recursive_scan']
# is enabled) is a candidate UNLESS its extension is obviously binary, or it
# fails UTF-8/Latin-1 decoding, or it is empty, or it contains a NUL byte.
# Extensions below are skipped outright without attempting to decode them
# (a fast-path optimisation only -- they are not the primary filter).
_BINARY_EXT_BLOCKLIST = (
    ".zip", ".png", ".jpg", ".jpeg", ".gif", ".pdf", ".xlsx", ".xls",
    ".docx", ".doc", ".exe", ".dll", ".pyc", ".so", ".bin", ".tar", ".gz",
)

# Device-boundary marker patterns used by _split_multi_device_blob() to
# split a single file into 1+ device-config chunks (DESIGN.md section
# 4.1.1, decision 12).
#
# Two-tier strategy (see _split_multi_device_blob's docstring for the full
# rationale): the first 5 "strong" patterns each unambiguously mark the
# start of a NEW device's own ``show running-config`` capture banner. If
# ANY strong-pattern line is found anywhere in the file, every such line is
# treated as a device-start boundary and the trailing ``hostname`` pattern
# is NOT used at all (using both at once would double-split a single
# device's own banner+hostname preamble). The ``hostname`` pattern is a
# FALLBACK, used ONLY when the file has zero strong-pattern occurrences
# (e.g. configs pasted without their capture banner) -- and even then, only
# its 2nd-and-later occurrence counts as a boundary (the first hostname line
# belongs to the implicit first device, whose start is the top of the file).
#
# NOTE on real device output verified while implementing Phase 1a (config_
# converter/Input_data/sample1/*.txt samples): classic IOS/IOS-XE's actual banner is
# ``Current configuration : NNNN bytes`` with NO leading ``!`` -- pattern 2
# below intentionally does NOT require one (an earlier draft of this pattern
# required a leading ``!``, which never matches real output). NX-OS does NOT
# emit "Building configuration..." at all (its own banner is
# ``!Command: show running-config``) -- Phase 1b adds a dedicated NX-OS
# pattern below rather than relying on pattern 1.
_DEVICE_BOUNDARY_MARKERS = (
    r"^Building configuration\.\.\.",   # IOS / IOS-XE
    r"^Current configuration\s*:",      # IOS / IOS-XE
    r"^!!\s*IOS XR Configuration",      # IOS-XR
    r"^:\s*Saved",                      # ASA / FTD
    r"^ASA Version",                    # ASA / FTD
    r"^!Command:\s*show running-config",  # NX-OS (Phase 1b)
    r"^hostname\s+\S+",                 # fallback (see 2nd+ occurrence rule above)
)
_STRONG_BOUNDARY_PATTERNS = tuple(re.compile(p, re.IGNORECASE) for p in _DEVICE_BOUNDARY_MARKERS[:-1])
_HOSTNAME_BOUNDARY_PATTERN = re.compile(_DEVICE_BOUNDARY_MARKERS[-1], re.IGNORECASE)
# Two strong-pattern lines within this many lines of each other are treated
# as the SAME device's own multi-line capture banner, not two separate
# devices -- see _split_multi_device_blob()'s docstring.
_BOUNDARY_COALESCE_GAP = 5


def _load_config(
    path: Optional[pathlib.Path],
) -> Tuple[Dict[str, Any], Set[str]]:
    """Read config_converter_to_ns_config.json; only each key's 'value' is used.

    Returns ``(cfg, explicit_keys)``. Keys whose names start with ``_`` are
    documentation-only and are ignored. ``explicit_keys`` lists every
    non-underscore key present in the JSON so callers can tell whether a
    setting was deliberately overridden vs. left to defaults/auto-detection.
    """
    if not path or not path.is_file():
        return {}, set()
    try:
        with path.open(encoding="utf-8-sig") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[WARN] could not read config {path}: {exc}", file=sys.stderr)
        return {}, set()
    out: Dict[str, Any] = {}
    explicit: Set[str] = set()
    for key, blob in raw.items():
        if key.startswith("_"):
            continue
        explicit.add(key)
        if isinstance(blob, dict) and "value" in blob:
            out[key] = blob["value"]
        else:
            out[key] = blob
    return out, explicit


def _input_has_site_subdirectories(input_dir: pathlib.Path) -> bool:
    """True when ``input_dir`` holds decodable configs in 2+ immediate subdirs.

    Used to auto-enable ``site_scoping`` for the common "one subdirectory per
    site" layout (e.g. ``site_a/`` + ``site_b/`` under the input root)
  without requiring a separate scenario config file.
    """
    sites_with_config: Set[str] = set()
    for path in input_dir.glob("*/*"):
        if not path.is_file():
            continue
        if _is_probably_binary(path) or _is_repo_documentation_file(path):
            continue
        for encoding in ("utf-8-sig", "utf-8", "latin-1"):
            try:
                raw = path.read_text(encoding=encoding)
                break
            except (UnicodeDecodeError, LookupError, OSError):
                raw = None
        else:
            raw = None
        if raw is None or not raw.strip() or "\x00" in raw:
            continue
        sites_with_config.add(path.parent.name)
    return len(sites_with_config) >= 2


def _apply_site_scoping_default(
    input_dir: pathlib.Path,
    cfg: Dict[str, Any],
    explicit_keys: Set[str],
) -> None:
    """Auto-enable ``site_scoping`` for multi-site directory layouts, in place."""
    if "site_scoping" in explicit_keys:
        return
    if bool(cfg.get("recursive_scan", False)):
        return
    if _input_has_site_subdirectories(input_dir):
        cfg["site_scoping"] = True


def _split_multi_device_blob(raw_text: str) -> List[str]:
    """Split one file's raw text into 1+ per-device config chunks (DESIGN.md
    section 4.1.1, decision 12).

    Algorithm (see the two-tier rationale in the ``_DEVICE_BOUNDARY_MARKERS``
    comment above):
      1. Find every line matching one of the 5 "strong" boundary patterns.
      2. Coalesce strong-pattern lines that are within
         ``_BOUNDARY_COALESCE_GAP`` lines of each other into a single
         boundary (keeping only the first of each cluster). This matters
         because a single real device's OWN capture banner typically
         contains two-or-more strong-pattern lines close together (e.g.
         "Building configuration..." immediately followed a line or two
         later by "Current configuration : NNNN bytes") -- without
         coalescing, a genuinely single-device file would be mis-split at
         its own banner. Two DIFFERENT devices' banners, by contrast, are
         always separated by an entire device's worth of config (dozens of
         lines at minimum), so a small gap threshold cleanly disambiguates
         "same device's multi-line banner" from "start of a new device".
      3. If any strong-pattern boundary was found, those (coalesced) lines
         ARE the device-start boundaries and the ``hostname`` fallback is
         not consulted at all. Otherwise, fall back to ``hostname`` lines:
         the implicit first device starts at line 0, and each hostname
         occurrence AFTER the first one starts a new device.
      4. If the resulting boundary list has 2+ entries, slice the file
         between consecutive boundaries (last chunk runs to EOF) and return
         those chunks. Otherwise return ``[raw_text]`` unchanged -- the
         existing "one file, one device" behaviour is fully backward
         compatible and no reformatting/trimming is applied in that case.
      5. This function never raises -- if anything about the input is
         unexpected, it falls back to treating the whole file as one device
         (tolerant-parsing policy shared with cml_converter). An over-eager
         split merely produces one extra inferred device downstream
         (safe-side failure mode, DESIGN.md 4.1.1 risk note).
    """
    try:
        if not raw_text:
            return [raw_text]
        lines = raw_text.splitlines(keepends=True)

        raw_strong_positions = [
            i for i, line in enumerate(lines)
            if any(pat.match(line) for pat in _STRONG_BOUNDARY_PATTERNS)
        ]
        strong_positions: List[int] = []
        for pos in raw_strong_positions:
            if not strong_positions or pos - strong_positions[-1] > _BOUNDARY_COALESCE_GAP:
                strong_positions.append(pos)
        if strong_positions:
            boundaries = strong_positions
        else:
            hostname_positions = [
                i for i, line in enumerate(lines) if _HOSTNAME_BOUNDARY_PATTERN.match(line)
            ]
            boundaries = [0] + hostname_positions[1:] if hostname_positions else [0]

        if len(boundaries) <= 1:
            return [raw_text]

        chunks: List[str] = []
        for idx, start in enumerate(boundaries):
            end = boundaries[idx + 1] if idx + 1 < len(boundaries) else len(lines)
            chunk = "".join(lines[start:end])
            if chunk.strip():
                chunks.append(chunk)
        return chunks if len(chunks) >= 2 else [raw_text]
    except Exception:
        return [raw_text]


def _is_probably_binary(path: pathlib.Path) -> bool:
    """Fast-path extension check only (DESIGN.md 4.1.1) -- NOT the primary
    text/binary filter (see _load_config_directory's decode-based filter)."""
    return path.suffix.lower() in _BINARY_EXT_BLOCKLIST


def _is_repo_documentation_file(path: pathlib.Path) -> bool:
    """Skip this repo's own ``Input_data/README.md``-style documentation file
    (discovered as a real edge case while implementing Phase 1a: decision
    12's "no extension whitelist" otherwise happily ingests it as a bogus
    single-interface-less "device", since it is valid, non-empty, decodable
    text). This is a narrow, name-based exception for OUR OWN repo
    convention (every converter's ``Input_data/`` ships a ``README.md``) --
    it is NOT a general extension whitelist and does not affect any
    user-supplied file, however named."""
    return path.stem.lower() == "readme"


def _load_config_directory(input_dir: pathlib.Path, cfg: Dict[str, Any]) -> Dict[str, str]:
    """Scan ``input_dir`` for running-config text files and split any
    multi-device files into individual per-device chunks.

    DESIGN.md 4.1.1, decision 12 -- confirmed design, no extension
    whitelist:
      1. Enumerate every file under ``input_dir``: recursively (any depth)
         when ``cfg['recursive_scan']`` is True, otherwise the top-level
         files PLUS exactly one level of subdirectories (so a flat layout
         and a "one subdirectory per site" layout both work without extra
         configuration). When not recursing and ``cfg['site_scoping']`` is
         enabled, each such immediate subdirectory name is folded into the
         returned label as a ``<site>/<stem>`` prefix, so a later phase's
         ``topology_mapper.build_subnet_groups()`` can recover the site hint
         from ``ParsedConfig.source_filename`` (DESIGN.md section 4.3.6)
         without this function needing to return a second, parallel
         structure.
      2. Skip any file whose extension is in ``_BINARY_EXT_BLOCKLIST``
         (fast path).
      3. For every remaining file, attempt to decode as utf-8-sig, then
         utf-8, then latin-1; skip if all three fail, the content is empty,
         or it contains a NUL byte (silently -- Phase 1a scope; wiring a
         skip-reason into config_report.md is deferred alongside the rest of
         the report-writing work, DESIGN.md section 4.1.1).
      4. Pass each successfully-decoded file's raw text through
         _split_multi_device_blob(). For a file that split into N>=2
         chunks, key them as ``<label>__1``, ``<label>__2``, ... ; a file
         that did not split keeps the plain ``<label>`` key (unchanged,
         fully backward compatible with "one file, one device").
      5. OS-platform identification is NEVER manual -- it is always
         performed per chunk by config_parser.detect_os_family() downstream
         in parse_all(), not in this function.
    Returns {label: raw_text}, matching the shape cml_converter's
    ``_load_running_configs()`` / ``parse_all()`` expect.
    """
    out: Dict[str, str] = {}
    if not input_dir.is_dir():
        return out

    recursive_scan = bool(cfg.get("recursive_scan", False))
    site_scoping = bool(cfg.get("site_scoping", False))

    if recursive_scan:
        candidates = sorted(p for p in input_dir.rglob("*") if p.is_file())
    else:
        candidates = sorted(p for p in input_dir.glob("*") if p.is_file())
        candidates += sorted(p for p in input_dir.glob("*/*") if p.is_file())

    for path in candidates:
        if _is_probably_binary(path) or _is_repo_documentation_file(path):
            continue

        raw_text: Optional[str] = None
        for encoding in ("utf-8-sig", "utf-8", "latin-1"):
            try:
                raw_text = path.read_text(encoding=encoding)
                break
            except (UnicodeDecodeError, LookupError):
                continue
            except OSError:
                break
        if raw_text is None or not raw_text.strip() or "\x00" in raw_text:
            continue

        rel = path.relative_to(input_dir)
        stem = rel.with_suffix("").as_posix()
        if not recursive_scan and site_scoping and len(rel.parts) > 1:
            label_base = stem  # already "<site>/<filename_stem>" via as_posix()
        else:
            label_base = pathlib.PurePosixPath(stem).name if len(rel.parts) > 1 else stem

        chunks = _split_multi_device_blob(raw_text)
        if len(chunks) == 1:
            out[label_base] = chunks[0]
        else:
            for i, chunk in enumerate(chunks, start=1):
                out[f"{label_base}__{i}"] = chunk

    return out


def _write_csv(path: pathlib.Path, rows: List[List[str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(rows)


def _write_report(path: pathlib.Path, report, n_commands: int) -> None:
    """TODO: adjust the fields read from `report` to match
    topology_mapper.MapperReport, and make sure every DESIGN.md
    4.3/4.5/4.6/4.7/4.8 decision that carries a confidence/ambiguity flag is
    represented here — this file is what lets a user audit an inferred
    link/device/WAN-classification/closed-environment verdict instead of
    silently trusting it (see DESIGN.md sections 4.3.3, 4.3.9, 4.5, 4.6,
    4.7.1, 4.8)."""
    lines = [
        "# config_converter -> Network Sketcher — Conversion Report\n",
        "## Counts\n",
        f"- **devices**: {report.devices}",
        f"- **l1_links (matched from same-subnet pairing)**: {report.l1_links}",
        f"- **inferred_peers (requirement E)**: {report.inferred_peers}",
        f"- **inferred_l2_switches (requirement C L2 inference gate, DESIGN.md 4.3.3)**: {report.inferred_l2_switches}",
        f"- **ambiguous_matches (requirement C tie-breaks)**: {report.ambiguous_matches}",
        f"- **excluded_links (RFC1918 cross-site conflicts -> config_excluded_links.csv, DESIGN.md 4.3.9)**: {report.excluded_links}",
        f"- **wan_interfaces (requirement H)**: {report.wan_interfaces}",
        f"- **closed_interfaces (requirement G)**: {report.closed_interfaces}",
        f"- **closed_devices_isolated (placed in the isolated area, DESIGN.md 4.7.1)**: {report.closed_devices_isolated}",
        f"- **inferred_connectivity_links (requirement F full-connectivity guarantee, DESIGN.md 4.6)**: {report.inferred_connectivity_links}",
        f"- **network_groups (final connected components after requirement F, requirement D, DESIGN.md 4.4)**: {report.network_groups}",
        f"- **NS commands emitted**: {n_commands}",
        "",
        "## Notes / accuracy caveats\n",
    ]
    for note in report.notes:
        lines.append(f"- {note}")
    lines.append("")
    if getattr(report, "closed_device_details", None):
        lines.append("## Isolated devices (requirement G, DESIGN.md 4.7.1)\n")
        for detail in report.closed_device_details:
            lines.append(f"- {detail}")
        lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Convert a directory of Cisco running-configs into a Network Sketcher command script.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--input", "-i", required=True, metavar="CONFIG_DIR",
                        help="Directory containing one running-config text file per device.")
    parser.add_argument("--out", "-o", default="ns_commands.txt", metavar="OUTPUT_FILE",
                        help="Base output path; side-outputs go to its directory. "
                             "Default: ns_commands.txt")
    parser.add_argument("--config", "-c", default=None, metavar="CONFIG_JSON",
                        help="Path to config_converter_to_ns_config.json (optional).")
    args = parser.parse_args(argv)

    input_dir = pathlib.Path(args.input).resolve()
    out_path = pathlib.Path(args.out).resolve()
    out_dir = out_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = out_path.stem or "ns_commands"
    cfg, explicit_keys = _load_config(
        pathlib.Path(args.config).resolve() if args.config else None
    )
    _apply_site_scoping_default(input_dir, cfg, explicit_keys)

    print(f"[1/4] Scanning config directory: {input_dir}")
    raw_configs = _load_config_directory(input_dir, cfg)
    print(f"        {len(raw_configs)} file(s) found")

    print("[2/4] Parsing running-configs ...")
    parsed = parse_all(raw_configs)

    print("[3/4] Inferring topology + building NSModel ...")
    model, mappings, report = build_model(parsed, cfg)
    script = build_command_script(model)
    n_commands = sum(script.counts.values())

    print("[4/4] Writing outputs ...")
    script_name = f"{stem}_config.txt"
    (out_dir / script_name).write_text(script.text(), encoding="utf-8")
    (out_dir / "ns_model_config.json").write_text(
        json.dumps(model_to_dict(model), indent=2, ensure_ascii=False), encoding="utf-8")
    _write_csv(out_dir / "config_inventory.csv", to_csv_rows(mappings))
    _write_report(out_dir / "config_report.md", report, n_commands)
    # DESIGN.md 4.3.9, decision 5: report.excluded_candidates (populated by
    # build_model() via infer_l1_links_from_subnets()) is the list of RFC1918
    # cross-site conflicts with no distinguishing evidence. Always write the
    # file (even if empty, header-only) so its presence/absence is not itself
    # a signal a user has to infer.
    write_excluded_links_csv(report.excluded_candidates, out_dir / "config_excluded_links.csv")

    print(f"  devices={report.devices} l1_links={report.l1_links} commands={n_commands}")
    for note in report.notes:
        print(f"  [note] {note}")
    print(f"  -> {script_name}, ns_model_config.json, config_inventory.csv, config_report.md, config_excluded_links.csv")
    print("\n[Done] All outputs written to:", out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
