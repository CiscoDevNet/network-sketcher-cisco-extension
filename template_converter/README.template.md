<!--
TEMPLATE FILE — copy to <platform>_converter/README.md and fill in every
<PLACEHOLDER>. Read GUIDE.md's "README convention" section first.

HARD RULE: this document is for a USER who wants to run your converter
against their own data/API — not for a contributor reading your source code.
Do not include: internal module/function names, "How it works" pipeline
diagrams naming your .py files, contribution/PR-process notes, or internal
QA-methodology narrative ("validated against a synthetic export because our
lab didn't have X enabled"). A short "validated end-to-end against a live
<platform>" statement is fine; a breakdown of which specific lab conditions
were/weren't exercised is not. See the removed sections listed at the bottom
of this template for real examples of what got cut from this repo's own
READMEs for violating this rule.

Sections marked OPTIONAL are for converters that need the dual
underlay/overlay split (see aci_converter/catc_converter/nd_converter) or
have some other genuine structural reason to add them — don't include a
section just because another converter has one.
-->

# <platform>_converter — <Platform Name> to Network Sketcher Command Converter

Convert a [<Platform Name>](<official product URL>) <one-line description of
what it manages> into a ready-to-run
[Network Sketcher](https://github.com/cisco-open/network-sketcher) command
script. <One or two sentences on what layers/objects get reconstructed —
L1/L2/L3, or "physical underlay + logical overlay" if dual-mode.>

<!-- OPTIONAL, only if your platform has a read-only API: one line stating
acquisition is API-based and (if dual-mode) that fetch and convert are
separate steps. -->

<!-- OPTIONAL, dual-mode only: a "## Why two diagrams: underlay vs. overlay"
section with the comparison table — see aci_converter/README.md or
nd_converter/README.md for the exact shape to copy. -->

## Overview

| Item | Detail |
|------|--------|
| **Input** | <how the user gets input: API fetch → JSON, or a specific export file format> |
| **Output** | <main output file(s)> + debug/audit artefacts |
| **Dependencies** | <Python version>, <stdlib only, or list the one real third-party package and why> |
| **<Platform> connectivity** | <only needed for the fetch step, if any; conversion itself is offline> |

## Quick Start

```bash
# 1. Install (state plainly whether this is a no-op or needs real packages)
pip install -r <platform>_converter/requirements.txt

<!-- OPTIONAL, API-fetch converters only: -->
# 2. Fetch the model over the read-only API (password via <PLATFORM>_PASSWORD env var)
<PLATFORM>_PASSWORD=... python -m <platform>_converter.src.fetch_from_<platform> \
    --host <platform>.example.com --user admin \
    --out <platform>_converter/Input_data/<platform>_export.json

# 3. Convert (offline; repeatable)
python -m <platform>_converter.src.convert \
    -i <platform>_converter/Input_data/<platform>_export.json \
    -o <platform>_converter/Output_data/ns_commands.txt \
    -c <platform>_converter/<platform>_to_ns_config.json
```

## Output files

| File | Description |
|------|-------------|
| `<stem>_<platform>.txt` | Network Sketcher CLI commands (Phase 1–6) — main deliverable |
| `ns_model_<platform>.json` | Intermediate topology model for debugging |
| `<platform>_inventory.csv` | Device → stencil-type mapping (audit) |
| `<platform>_report.md` | Counts + accuracy caveats (what's observed vs. synthesized) |

<!-- OPTIONAL, dual-mode only: a "## Modes" table + the two output-files rows
per mode, and "## <Platform> → Network Sketcher mapping" tables (underlay +
overlay) — see aci_converter/README.md / nd_converter/README.md. -->

<!-- OPTIONAL: a "## ⚠️ Concepts that can NOT be fully represented" callout
if your platform has a logical model that doesn't map 1:1 onto Network
Sketcher's VLAN/port model (every dual-mode converter has one — see
aci_converter/README.md for the pattern). -->

## Device color conventions

The generated `rename attribute_bulk` command writes a coloured cell into the
**Default** column of the Network Sketcher Attribute sheet —
`"['DEVICE',[R,G,B]]"` (WayPoints keep their token: `"['WayPoint',[R,G,B]]"`)
— so every device is colour-coded by role in the Device Table. The base
palette is **shared across every converter in this repo** — reuse it exactly,
do not invent new colours:

| Colour | RGB | Meaning |
|--------|-----|---------|
| 🟩 Light green | `[235, 241, 222]` | **Observed network gear** — real router / L3 switch / switch / firewall / WLC / AP |
| 🟥 Light red | `[255, 204, 204]` | **Server-role endpoint** |
| 🟨 Light yellow | `[255, 255, 204]` | **Client endpoint** — PC / workstation / phone |
| 🟪 Light purple | `[221, 204, 255]` | **Logical / overlay** — dual-mode converters only: every synthesized overlay device |
| 🟦 Light blue | `[220, 230, 242]` | **Observed WayPoint** — a WayPoint backed by a real source record (not purely invented by the converter). Also the fixed colour of the **Stencil Type** attribute column |
| ⬜ Light gray | `[200, 200, 200]` | **Inferred / synthesised** — placeholders that do not exist in the source, including any purely-invented WAN/Internet/cloud **WayPoint** |

Two further fixed cell colours appear in every device row (set by the shared
`ns_command_builder`, not role-based): the **Model** column is pink
`[255, 183, 219]` and the **OS** column is light blue `[200, 230, 255]`.

<!-- Delete the purple row above if your converter is single-mode (no
overlay). State plainly in one sentence which of these rows your converter
actually emits. -->

<!-- OPTIONAL, API-fetch converters only: a "## `fetch_from_<platform>.py` —
pulling the model over the REST API" section stating the auth mechanism,
that retrieval is read-only, and any paging/optional-source behaviour. See
GUIDE.md "Credential & security conventions" for what this MUST state
(env-var credentials, read-only, TLS default) and MUST NOT state (internal
response-unwrapping details, "please report parsing issues"). -->

<!-- OPTIONAL, dual-mode only: an "### AI-agent runbook" subsection under
"## Running the output in Network Sketcher" — copy verbatim from
aci_converter/README.md / catc_converter/README.md / nd_converter/README.md
if your converter has multiple modes that get built into separate MCP
workspaces. Single-mode converters don't need this. -->

## Configuration (`<platform>_to_ns_config.json`)

Every key is `{value, description, sample}`; only `value` is read. Key
options: <one line per key, or a short table — see meraki_converter/README.md
or netbox_converter/README.md for the two accepted styles>.

## Author

<Your name / handle> - <role>, <organization>

## License

This tool is part of the **Network Sketcher Cisco Extension** project,
licensed under the [Apache License 2.0](../LICENSE) <!-- use ../../LICENSE
if this converter lives two levels deep under 3rd_party/ -->. See the
[NOTICE](../NOTICE) file for copyright and third-party attributions.

<!--
Sections that were REMOVED from this repo's real converter READMEs for being
contributor-facing rather than user-facing — do not re-add anything like
these (see GUIDE.md "README convention" for the full story):
  - "## Getting involved" (contribution/PR workflow — belongs in
    CONTRIBUTING.md only)
  - "## How it works" pipeline diagrams naming internal .py modules/functions
  - "## Directory structure" trees that enumerate src/*.py internals
  - Detailed per-endpoint verification matrices citing raw API paths
  - "please report any export that does not parse" / "field names vary
    across releases so the parsers are tolerant" — internal QA/maintainer
    narrative, not something a user needs to run the tool
-->
