# config_converter — Cisco running-configs → Network Sketcher Command Converter

Convert a directory of Cisco device running-configuration text files
(`show running-config` output — one device per file, multiple devices
concatenated in one file, or a mix of both) into a ready-to-run
[Network Sketcher](https://github.com/cisco-open/network-sketcher) command
script — entirely from local files, with no live device connection needed at
conversion time.

> [!NOTE]
> **Status: Phases 1a–5 complete.** The full pipeline (config parsing →
> subnet-based topology inference → layout/tiering → inferred-device
> synthesis → WAN/closed-environment classification → Network Sketcher
> command generation) is implemented and verified end-to-end against a live
> Network Sketcher instance. See `DESIGN.md` section 6 for the phase log and
> section 5 for edge cases discovered during live verification.

---

## Overview

| Item | Detail |
|------|--------|
| **Input** | Any readable running-config text file(s) under a directory — one device per file, multiple devices per file, or a mix; optional per-site subdirectories |
| **Output** | `<stem>_config.txt` (Network Sketcher CLI commands, Phase 1–6) plus debug/audit artefacts |
| **Dependencies** | Python 3.10+, **networkx** (required); **ciscoconfparse2** (optional, improves parsing) |
| **Platform connectivity** | None — purely local file I/O |

### Supported platforms

| OS family | Notes |
|-----------|--------|
| IOS / IOS-XE | IOS-XE "Denali" and later (16.x/17.x). **Legacy IOS-XE 3.x is out of scope** (EoL); such configs may be misclassified as classic IOS. |
| NX-OS | **Detection:** Nexus `show running-config` exports are recognised by the `!Command: show running-config` header and Nexus-only keywords (`vdc`, `feature nv overlay`, or other `feature` lines). **Extracted for topology:** SVIs, physical Ethernet ports, `mgmt0`, nested `hsrp <group>` virtual IPs (not IOS-style single-line `standby`), sequence-numbered `ip access-list` rules, `policy-map type qos` bandwidth-limit signals, external BGP peers, and `vpc domain` / `vpc peer-link` (vPC peer-links between two Nexus switches present in the same input batch). **Not in scope:** DCNM/FMC-managed objects, ACI fabric policy, or a full NX-OS QoS/policy model — only plain running-config text is read. |
| IOS-XR | **Detection:** the `!! IOS XR Configuration` banner (or `rp/0/...`-style prompts near the file head) classifies the file as IOS-XR. **Extracted for topology:** `Bundle-Ether<n>` LAG interfaces (IOS-XR's Port-channel equivalent — not `Port-channel` naming), `MgmtEth0/RP0/CPU0/0`-style management ports (normalised for Network Sketcher), `ipv4 address` / `ipv4 access-group`, sequence-numbered `ipv4 access-list`, per-interface `vrf`, `bundle id` member ports, and nested `neighbor <ip>` / `remote-as <asn>` BGP blocks. Candidate (uncommitted) config is not supported — only finalised `show running-config` text. |
| ASA(FTD/FDM) | ASA, FMC-managed FTD, and FDM-managed FTD share one ASA/LINA parsing path, displayed as **"ASA(FTD/FDM)"** |

#### Out of scope (by design)

The converter rebuilds a **starting L1/L2/L3 reference diagram** from static
running-config text. The following are intentionally **not** supported in the
current release (see `DESIGN.md` §4.2–§4.8 and the Phase 6 roadmap):

- **No live device or API connection** — input is running-config text files only
  (no SSH, SNMP, APIC, FMC/FDM REST, or DCNM API at conversion time).
- **No CDP/LLDP neighbour data** — links are inferred from IPv4 subnet
  matching, not from neighbour tables. Pure L2 trunk/access ports with no L3
  address cannot be discovered on their own (IP reachability only). CDP/LLDP
  auxiliary input is a **Phase 6** optional extension.
- **No explicit cabling** — there is no port-to-port wiring in a running-config;
  topology is guessed from shared subnets (plus degree-similarity / Blossom
  matching for ambiguous cases).
- **Partial config bundles** — if a peer device is missing from the input set,
  the converter invents gray `Dummy_<TYPE>_<n>` placeholder peers rather than
  leaving the link empty.
- **Advanced firewall / policy GUI objects** — Zone-Based Firewall (ZBFW),
  `zone-pair`, `class-map type inspect`, FMC/FDM threat-defense policies, and
  similar policy-layer features are not parsed (high misclassification risk).
- **Full routing topology** — BGP/OSPF/EVPN adjacencies are not modelled; only
  light signals (e.g. external BGP neighbour, WAN interface hints) feed stencil
  and WAN scoring.
- **NAT object resolution, full ACL simulation, strict reachability proof** —
  ACL bodies are used only for conservative "closed environment" hints when the
  full ACL is present in the same file; object-groups, cross-device ACL
  templates, and path-aware reachability are not evaluated.
- **IPv6-only configs** — parsing and subnet matching are IPv4-first (RFC 1918 /
  RFC 5737 documentation addresses in bundled samples).

Because running-config bundles contain **no explicit cabling**, the topology is
**inferred** from same-subnet IP matching (plus degree-similarity / Blossom
matching for ambiguous shared subnets). Treat the output as a starting
reference — always validate against LLDP/CDP, DCIM, or physical inspection.

---

## Quick Start

```bash
# 1. Install dependencies (networkx is required)
pip install -r config_converter/requirements.txt

# 2. Convert the bundled reference sample (13 devices, all OS families)
python -m config_converter.src.convert \
    -i config_converter/Input_data/sample1/ \
    -o config_converter/Output_data/ns_commands.txt

# 3. Optional: pass the documented config JSON explicitly
python -m config_converter.src.convert \
    -i config_converter/Input_data/sample1/ \
    -o config_converter/Output_data/ns_commands.txt \
    -c config_converter/config_converter_to_ns_config.json

# 4. Convert your own config directory
python -m config_converter.src.convert \
    -i /path/to/your/configs/ \
    -o config_converter/Output_data/ns_commands.txt
```

`-c/--config` is optional. Omit it to run with built-in defaults (equivalent to
the `value` fields in `config_converter_to_ns_config.json`). If you **do**
pass `-c`, use a **complete** copy of that file with only the keys you want
to change — partial configs fall back to Python module defaults for missing
keys (not the JSON defaults), which can silently diverge for compound keys
like `wan_signal_weights`.

**Multi-site layouts:** when the input directory has config files in two or
more immediate subdirectories (e.g. `site_a/` + `site_b/` under your input
root), `site_scoping` is **auto-enabled** unless you pass `-c` with an
explicit `site_scoping` entry. No separate scenario config file is needed.

Then load the resulting `*_config.txt` into Network Sketcher (app or MCP
`run_commands`) to render the diagram.

---

## Output files

Written to the same directory as `--out`:

| File | Description |
|------|-------------|
| `<stem>_config.txt` | Network Sketcher CLI commands (Phase 1–6) — main deliverable |
| `ns_model_config.json` | Intermediate topology model (debug) |
| `config_inventory.csv` | Device → stencil mapping with `confidence` and `reason` (audit) |
| `config_report.md` | Counts, inferred links/devices, WAN/closed verdicts, skip notes |
| `config_excluded_links.csv` | Cross-site RFC1918 collisions excluded from the diagram (`site_scoping`) |

Every inferred device uses the naming convention `Dummy_<TC>_<n>` (e.g.
`Dummy_L2_1`, `Dummy_RT_2`, `Dummy_CL_1`) with inferred ports `Dummy 0`,
`Dummy 1`, … — see `DESIGN.md` section 4.5.1.

### Area grouping (L1 diagram)

- **Non-waypoint connected components** each become their own side-by-side
  area. Waypoints (`Dummy_CL_*`, `stencil_type == NS_CLOUD`) do **not** bridge
  separate device groups. Single component → `default`; multiple → `segment_NN`.
- **Linkless devices** (no L1 wiring) and **fully-closed** devices (requirement
  G) share one dedicated area (`isolated_area_name`, default **`"Closed"`**),
  sorted rightmost.
- **Waypoints** render in a top-row `*_wp_` cloud area; multiple waypoint
  devices in one area are laid out horizontally.

See `DESIGN.md` sections 4.4.1 and 4.7.2.

---

## Bundled sample data

The repository ships one reference input set under **`Input_data/sample1/`** —
seven files covering **13 synthetic devices** across IOS, IOS-XE, NX-OS, IOS-XR,
and ASA(FTD/FDM) (RFC 5737 / RFC 1918 documentation addresses). See
`Input_data/sample1/README.md` for the per-file breakdown and the expected
conversion counts.

`Input_data/` is the root folder for your own exports: add additional sample
subdirectories (e.g. `Input_data/site_a/`, `Input_data/production/`) or point
`--input` at any directory on disk. Outputs are written beside `--out`
(default: `Output_data/`).

---

## Device color conventions

The generated `rename attribute_bulk` command colour-codes the **Default**
column in the Network Sketcher Device Table (shared palette across this repo):

| Colour | RGB | Meaning |
|--------|-----|---------|
| 🟩 Light green | `[235, 241, 222]` | **Observed network gear** — device found in an input running-config |
| 🟥 Light red | `[255, 204, 204]` | **Server-role endpoint** |
| 🟨 Light yellow | `[255, 255, 204]` | **Client endpoint** — PC / phone |
| ⬜ Light gray | `[200, 200, 200]` | **Inferred / synthesised** — placeholder peer, WAN cloud, or connectivity invented by the converter |

Always read `config_report.md` before trusting a diagram — every gray element
and ambiguous match is listed with reasoning.

---

## Configuration

All tunables live in **`config_converter_to_ns_config.json`** (single config
file; schema version **0.51**; the former `phase5_scenarios_config.json` was
removed and its `site_scoping` scenario is now auto-detected for
multi-subdirectory inputs).

| Key area | Examples |
|----------|----------|
| Subnet matching | `matching_algorithm`, `shared_subnet_strategy`, `l2_inference_enabled` |
| Cross-site | `site_scoping` (auto-detected for multi-subdir layouts) |
| WAN scoring | `wan_signal_weights`, `wan_confidence_threshold`, `wan_interface_overrides` |
| Isolation | `closed_environment_detection`, `isolated_area_name` (default `"Closed"`) |
| Connectivity policy | `assume_fully_connected` (default `true`) |
| Overrides | `stencil_overrides`, `color_overrides`, `role_keyword_overrides` |

Each key documents its default, effect, and a `sample` value. See `DESIGN.md`
section 7 for the full schema rationale.

---

## Running the output in Network Sketcher

`*_config.txt` is a plain-text script (one command per line; `#` lines are
phase comments) ordered Phase 1→6. Create an empty master, run the non-comment
lines in order, then export the diagram. See the
[top-level README "Running the output in Network Sketcher"](../README.md#running-the-output-in-network-sketcher)
for step-by-step instructions and MCP workflow (`create_empty_master` →
`run_commands` → `build_default_outputs`).

---

## Directory structure

```
config_converter/
├── README.md                          (this file)
├── DESIGN.md                          (full design + risk register)
├── requirements.txt
├── config_converter_to_ns_config.json (sole configuration file)
├── Input_data/                        (input root; bundled sample in sample1/)
│   └── sample1/                       (Phase-1 reference — 7 files / 13 devices)
├── Output_data/                       (default output; gitignored)
└── src/                               (entry point: convert.py)
```

---

## Known live-engine constraints

Documented in `DESIGN.md` sections 5–6; the converter works around all of
these automatically:

- Ports only register with the engine after a successful `add l1_link_bulk`.
- Devices with zero L1 links skip deferred L2/L3 sync.
- An SVI already used as an L1 endpoint cannot also be a `virtual_port_bulk`.

---

## Changelog

- **Config consolidation**: removed `phase5_scenarios_config.json` (duplicate of
  the main config with only `site_scoping: true`). Cross-site behaviour is now
  handled by **auto-detecting** multi-subdirectory layouts in `convert.py`.
  Documentation-only `_scenarios` / `_`-prefixed keys in
  `config_converter_to_ns_config.json` are ignored by the loader.
- **Repository layout**: bundled reference configs live under
  `Input_data/sample1/` (formerly at `Input_data/` root). Additional scenario
  folders can be added as siblings under `Input_data/`.
- **Feature (area grouping)**: non-waypoint connected components → separate
  `segment_NN` areas; linkless + closed devices → shared `"Closed"` area. See
  `DESIGN.md` §4.4.1.
- **Enhancement (Phase A layout)**: `stencil_tiers` consistency, bidirectional
  column sweep, wire-length tiebreak for cross-row neighbours. See `DESIGN.md`
  risk #31.
- **Enhancement (waypoint layout)**: `*_wp_` areas on a top row; multiple
  waypoint devices horizontal in one area. See `DESIGN.md` §4.7.2.
- **Bug fixes**: Port-channel L2 VLAN symmetry, trunk-all-VLAN fallback, vPC
  peer-link pairing, Dummy-side Port-channel mirroring, and others — see
  `DESIGN.md` section 5 for the full risk register (#17–#30).

---

## Author

Yusuke Ogawa - Architect, Cisco | CCIE#17583

## License

This project is licensed under the [Apache License 2.0](../LICENSE). See the
[NOTICE](../NOTICE) file for copyright and third-party attributions.
