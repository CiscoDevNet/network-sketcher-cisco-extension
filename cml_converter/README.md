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

The generated `rename attribute_bulk` command writes a coloured cell into the **Default** column of the Network Sketcher Attribute sheet — `\"['DEVICE',[R,G,B]]\"` (WayPoints keep their token: `\"['WayPoint',[R,G,B]]\"`) — so every device is colour-coded by role in the Device Table. The palette and its meaning are **shared across the sna / cv / cml converters**:

| Colour | RGB | Meaning |
|--------|-----|---------|
| 🟩 Light green | `[235, 241, 222]` | **Observed network gear** — a real router / L3 switch / switch / firewall / WLC / AP present in the source data |
| 🟥 Light red | `[255, 204, 204]` | **Server-role endpoint** — server / controller / OT asset / internet service |
| 🟨 Light yellow | `[255, 255, 204]` | **Client endpoint** — PC / workstation / phone |
| 🟦 Light blue | `[220, 230, 242]` | **Observed network-device WayPoint** — a WayPoint backed by a real, observed network device (reserved; not emitted today, planned for future use) |
| ⬜ Light gray | `[200, 200, 200]` | **Inferred / not observed** — devices synthesised by the converter to complete a plausible topology, plus inferred WAN / Internet / cloud **WayPoints** (no real device behind them) |

The two WayPoint colours separate **observed** WayPoints (blue, backed by a real network device — future) from **inferred** WayPoints (gray, abstract WAN / Internet / cloud edges).

**In cml_converter:** every device in a CML lab is observed, so network gear (Router / L3Switch / Switch / Firewall / WLC / AP) is **green**, servers are **red**, and PC / Phone endpoints are **yellow**. Gray is reserved for the WAN / Internet / cloud WayPoints that have no real device behind them.

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

## How it works

```
CML YAML
   └─► topology_mapper.py  ─► NSModel (dataclass)
         ├─ stencil_mapper.py  (node_definition → NS Stencil Type)
         └─ config_parser.py   (running-config → VLAN/SVI/IP data)

NSModel
   └─► ns_command_builder.py  ─► Phase 1-6 NS CLI commands
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
└── src/
    ├── __init__.py
    ├── convert.py         ← entry point
    ├── topology_mapper.py
    ├── stencil_mapper.py
    ├── config_parser.py
    └── ns_command_builder.py
```

## Cisco Technologies

This tool bridges two Cisco technologies:

- **Cisco Modeling Labs (CML)** — network simulation platform for creating
  virtual network topologies
- **Network Sketcher** — open-source Cisco tool for designing and documenting
  network topologies using an AI-native CLI

## License

Apache License 2.0 — see [LICENSE](../LICENSE).
