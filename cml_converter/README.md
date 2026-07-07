# cml_converter — CML YAML to Network Sketcher Command Converter

Convert a [Cisco Modeling Labs (CML)](https://developer.cisco.com/modeling-labs/)
topology YAML file into a ready-to-run
[Network Sketcher](https://github.com/cisco-open/network-sketcher) command
script — no CML server required.

> The Network Sketcher Offline edition has a built-in CML import, but it covers
> **Layer 1 only** (devices and physical links). `cml_converter` is the
> **extended version that also reconstructs Layer 2 and Layer 3** (VLANs, SVIs,
> sub-interfaces, port-channels, IP addresses and VRFs) from the running-configs.

---

## Overview

| Item | Detail |
|------|--------|
| **Input** | CML Lab YAML (UI export or API dump) + optional running-config files |
| **Output** | `ns_commands.txt` ready for Network Sketcher `run_commands`, plus debug/audit artefacts |
| **Dependencies** | Python 3.10+, PyYAML, ciscoconfparse2 (optional but recommended) |
| **CML connectivity** | None — purely local file I/O |

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Export your lab from the CML UI: Lab → Export → YAML
#    Save it as my_lab.yaml

# 3. Run the converter (run from the repository root)
python -m cml_converter.src.convert \
    --yaml  my_lab.yaml \
    --out   output/ns_commands.txt

# 4. Optional: also provide running-config files
python -m cml_converter.src.convert \
    --yaml    my_lab.yaml \
    --configs running_configs/ \
    --out     output/ns_commands.txt
```

## Output files

| File | Description |
|------|-------------|
| `ns_commands.txt` | Network Sketcher CLI commands (Phase 1–6) |
| `ns_model.json` | Intermediate topology model for debugging |
| `stencil_mapping.csv` | Device stencil-type mapping with confidence scores |
| `parse_report.md` | Per-device running-config coverage statistics |

## Device color conventions

The generated `rename attribute_bulk` command writes a coloured cell into the **Default** column of the Network Sketcher Attribute sheet — `\"['DEVICE',[R,G,B]]\"` (WayPoints keep their token: `\"['WayPoint',[R,G,B]]\"`) — so every device is colour-coded by role in the Device Table. The palette and its meaning are **shared across every converter in this repo**:

| Colour | RGB | Meaning |
|--------|-----|---------|
| 🟩 Light green | `[235, 241, 222]` | **Observed network gear** — a real router / L3 switch / switch / firewall / WLC / AP present in the source data |
| 🟥 Light red | `[255, 204, 204]` | **Server-role endpoint** — server / controller / OT asset / internet service |
| 🟨 Light yellow | `[255, 255, 204]` | **Client endpoint** — PC / workstation / phone |
| 🟦 Light blue | `[220, 230, 242]` | **Observed WayPoint** — a WayPoint backed by a real source record. Also the fixed colour of the **Stencil Type** attribute column |
| ⬜ Light gray | `[200, 200, 200]` | **Inferred / not observed** — devices synthesised by the converter to complete a plausible topology (none currently — see below) |

The two WayPoint colours separate **observed** WayPoints (blue, backed by a real node in the lab) from **inferred** WayPoints (gray, invented by the converter with no real node behind them).

Two further fixed cell colours appear in every device row (set by the shared `ns_command_builder`, not role-based): the **Model** column is pink `[255, 183, 219]` and the **OS** column is light blue `[200, 230, 255]`.

**In cml_converter:** every device in a CML lab is observed — including an `external_connector` node (the bridge to the host network), which is a real node the user placed in the lab, so it renders **light blue**, not gray. Network gear (Router / L3Switch / Switch / Firewall / WLC / AP) is **green**, servers are **red**, and PC / Phone endpoints are **yellow**. Gray is reserved for devices the converter itself would invent — cml_converter has no such case today, since every device comes from a real CML lab node.

## Running the output in Network Sketcher

`ns_commands.txt` is a plain-text script (one command per line; `#` lines are
phase comments) already ordered Phase 1→6. Install
[Network Sketcher](https://github.com/cisco-open/network-sketcher), create an
empty master, run the command lines in order against it, and export the diagram.
See the
[top-level README "Running the output in Network Sketcher"](../README.md#running-the-output-in-network-sketcher)
for step-by-step instructions.

## Running-config files

Place one text file per device under a directory and pass it with `--configs`.
File stems must **exactly** match the CML node `label` (case-sensitive).

```
running_configs/
├── spine1.txt
├── leaf1.txt
└── border1.txt
```

## Supported CML YAML formats

- CML UI export (`Lab → Export → YAML`) — `lab: {nodes: [...], links: [...]}`
- CML REST API dump (`GET /api/v0/labs/{id}/topology`) — flat `nodes`/`links`
- Single-file combined topology dumps with top-level `topology:` key

## Directory structure

```
cml_converter/
├── README.md            (this file)
├── requirements.txt
├── .gitignore
└── src/                 (entry point: convert.py)
```

## Changelog

- **Bug fix**: `topology_mapper.py`'s `assign_areas_and_rows()` passed a
  `default_color=` keyword argument to `NSDevice(...)` (intended to render an
  observed `external_connector` WayPoint in light blue instead of the
  inferred-WayPoint gray — see "Device color conventions" above) that the
  local `NSDevice` dataclass did not declare as a field. This raised
  `TypeError: __init__() got an unexpected keyword argument 'default_color'`
  for **every** CML lab, so `convert.py` could not complete a single
  conversion. Root cause: unlike the other converters in this repo,
  `cml_converter` defines `NSDevice` inline in `topology_mapper.py` instead of
  a separate `ns_model.py`; when the `default_color` override field (present
  in `template_converter/src/ns_model.py` and
  `3rd_party/netbox_converter/src/ns_model.py`) was reused here, the field
  declaration itself was never copied over. Fixed by adding the missing
  `default_color: Optional[Tuple[int, int, int]] = None` field to `NSDevice`
  and by making `ns_command_builder.cmd_rename_attribute_bulk()` actually
  consume it (a per-device colour override wins over the role-based colour),
  matching the pattern already used by `template_converter` /
  `netbox_converter`. Verified with a synthetic lab containing an
  `external_connector` node: `convert.py` now completes without exceptions
  and the generated `rename attribute_bulk` command colours the connector's
  `Default` cell `[220, 230, 242]` (light blue) instead of falling back to
  the generic WayPoint gray — confirmed live in Network Sketcher via the
  `network-sketcher` MCP (`row_colors` in the exported Device Table shows
  `rgb(220,230,242)` on the `inet_bridge` row).

- **Bug fix**: `stencil_mapper.py`'s keyword-heuristic fallback used to embed the
  matched keyword in single quotes when building the Model string (e.g.
  `Spine Switch (inferred from 'spine')`). Because the shared `ns_command_builder.py`
  escaping convention (`\'`) is not correctly round-tripped by the live Network
  Sketcher engine's `rename attribute_bulk` cell parser, this produced an
  `invalid syntax` warning at runtime and left the Model cell empty for every
  keyword-matched device. Fixed by dropping the quote characters instead of
  escaping them — the keyword is now embedded as plain text (e.g.
  `Spine Switch (inferred from spine)`). No change to `ns_command_builder.py`
  (shared across converters) or to the meaning of the Model string itself.

## Cisco Technologies

This tool bridges two Cisco technologies:

- **Cisco Modeling Labs (CML)** — network simulation platform for creating
  virtual network topologies
- **Network Sketcher** — open-source Cisco tool for designing and documenting
  network topologies using an AI-native CLI

## Author

Yusuke Ogawa - Architect, Cisco | CCIE#17583

## License

This tool is part of the **Network Sketcher Cisco Extension** project, licensed
under the [Apache License 2.0](../LICENSE). See the [NOTICE](../NOTICE) file for
copyright and third-party attributions.
