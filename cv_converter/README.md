# cv_converter — Cisco Cyber Vision → Network Sketcher

Generates an **OT network diagram** in Network Sketcher from Cisco Cyber Vision
CSV exports, laid out along the **Purdue model / CPwE / IEC 62443** zone
hierarchy (Enterprise → IDMZ → Industrial Zone → Cell/Area Zone).

Unlike `sna_converter` (which targets office/enterprise NetFlow and *infers*
sites), Cyber Vision already classifies assets into **Groups** with **Device
Types** and **Industrial Impact**, so this tool *maps* that structure onto an OT
reference architecture rather than guessing it.

> Status: **Phase 1–3 implemented and verified** against the live
> `network-sketcher` MCP engine (commands imported with 0 failures; all diagram
> artifacts generated).

---

## Inputs (one or both)

Both are semicolon-delimited CSVs exported from Cyber Vision. The tool
auto-detects each file by its header, or you can pass them explicitly.

| File | Role | Key columns used |
|------|------|------------------|
| `networkNodes-*.csv` | **Assets / nodes** | Device Type, Group, IP, MAC, Vendor, OS, Model, Firmware, Risk Score, Vuln, VLAN ID |
| `activities-*.csv` | **Communications / edges** | Component 1/2 Name·Group·Industrial Impact·IP·MAC, Tags, Bytes, Packets |

Using **both** gives the richest result (assets supply attributes & Device Type;
activities supply the flows). Either one alone also works.

## Outputs (written to the output directory)

| File | Contents |
|------|----------|
| `gen_master_commands.txt` | Network Sketcher CLI script (Phase 1–6: areas → devices → L1 links → port info → VLAN/SVI/L2 → IP → attributes) |
| `gen_flow_list.csv` | `[Flow_List]` paste sheet — src/dst device, protocol, service(port from Tags), Max bandwidth (Mbps; see caveat below) |
| `gen_zone_assignment.csv` | Each CV Group → assigned Purdue zone + decision basis (review this!) |
| `gen_conduit_report.csv` | IEC 62443 cross-zone conduit analysis (flags Enterprise↔OT that bypasses the IDMZ) |
| `out_of_scope.csv` | Excluded noise groups, non-routable IPs, dropped duplicate IPs — with reasons |

## Usage

```bash
# No third-party dependencies (Python 3.8+ stdlib only).
python cv_to_ns_commands.py                       # auto-detect CSVs in the default folder
python cv_to_ns_commands.py --input-dir <dir>     # auto-detect in a folder
python cv_to_ns_commands.py --nodes nodes.csv --activities acts.csv --output-dir <dir>
```

Defaults (overridable): `--input-dir` is `Input_data` and `--output-dir`
defaults to the input dir; config is `cv_to_ns_config.json` (next to the script).

### Build the actual diagram (via the network-sketcher MCP)

```
set_workspace("~/ns_cv_demo")           # workspace must be under the home dir
create_empty_master("[MASTER]cv_demo.nsm")
get_ai_context("[MASTER]cv_demo.nsm")   # required once before run_commands
run_commands("[MASTER]cv_demo.nsm", <contents of gen_master_commands.txt>)
build_default_outputs("[MASTER]cv_demo.nsm")
```

Produces the tabbed **L1/L2/L3 HTML viewer**, three layer SVGs, and the Device
Table HTML.

## Zone classification (minimal-auto + config override)

Each CV Group is assigned to one of `ENTERPRISE | IDMZ | INDUSTRIAL | CELL` by:
1. **`group_zone_override`** in the config (authoritative — pin site-specific names here);
2. a **minimal keyword** match on the group name (e.g. `enterprise`, `engineering station`, `furnace`, `process bus`);
3. otherwise the **majority Device Type level** of the group's assets (L3 → Industrial, L0–L2 → Cell/Area).

Device Type also drives the Network Sketcher stencil (Controller/IO/Master →
Server, SCADA/Engineering/Windows → PC, Remote Access Gateway → Firewall,
Routing Capability → Router) and the per-asset Purdue level.

Noise groups (`Broadcast Components`, `Multicast`, `IPv6 Components`,
`Packet Reply`, `To be investigated`, ungrouped) and non-routable IPs are
excluded by default — toggle with `exclude_noise_groups` in the config.

## CPwE infrastructure synthesis

Cyber Vision does not export physical cabling, so the tool synthesises a
CPwE-style logical backbone (toggle `synthesize_infrastructure`):

```
[Enterprise-Zone]  ENT-Core (Catalyst 9500)
        |
   IDMZ-FW (Secure Firewall 3100)        <- the Enterprise↔Industrial conduit
        |
[Industrial-Zone]  IND-Core (Catalyst IE9300)  --- L3 site-ops assets + SVIs
        |  \  \
[Cell/Area Zones]  CELLSW-<cell> (Catalyst IE3400) per CV cell, uplinked to IND-Core
```

Each area gets one VLAN + /24 (gateway SVI on the nearest L3 device); endpoints
take their real IP on their access port (RULE 11.5).

> [!IMPORTANT]
> **Areas (zones) are wired together with direct L1 links — do NOT insert
> WayPoints between them.**
> In a general enterprise diagram, two areas are usually bridged through a
> Network Sketcher **WayPoint** (a `WAN` / `Internet` cloud). This converter
> deliberately does **not** do that for OT: under the Purdue / IEC 62443 / CPwE
> model the zone boundaries are themselves the security construct (the
> Enterprise↔Industrial conduit is the **`IDMZ-FW`**, cells uplink straight to
> **`IND-Core`**), so the zones are connected by **direct L1 cabling** between
> their infrastructure devices. Adding a WayPoint-based inter-area connection on
> top of this is **discouraged** — it would hide the very conduit boundary the
> diagram is meant to make explicit and misrepresent the zoning.

## Access switches (ENT-Edge / IND-Edge)

Host endpoints never connect directly to a core. Each of the Enterprise and
Industrial zones gets a synthesised **access L2 switch** placed to the **left of
its core**, with the hosts hanging off it (mirrors the cell access switches):

- **`ENT-Edge`** ← Enterprise hosts; uplinks to `ENT-Core`.
- **`IND-Edge`** ← Industrial hosts; uplinks to `IND-Core`.

Only `ENT-Edge`/`IND-Edge`, `IDMZ-FW` and the cell switches connect to a core
directly. Layout follows the backbone direction: **`ENT-Core` sits at the bottom
of the Enterprise band** (facing the IDMZ below it), while `IND-Core` sits at the
top of the Industrial band (facing the IDMZ above it).

## IDMZ straddlers (IT/OT bridges)

Any asset in the Activity list that communicates with **both** the Enterprise
zone and the Industrial zone is, per IEC 62443 / CPwE, an IT/OT bridge that
belongs in the Industrial DMZ. The converter:

1. relocates such assets into the **IDMZ** zone, and
2. synthesises a DMZ L2 switch **`IDMZ-SW`**, placed to the **left of `IDMZ-FW`**,
   with the straddlers hung **below** it. `IDMZ-SW` uplinks to `IDMZ-FW`, which
   hosts the DMZ gateway SVI.

The straddler count (and device names) are printed in the run summary. Tune the
behaviour with `idmz_straddler_detection`, `idmz_enterprise_zones` and
`idmz_industrial_zones` in `cv_to_ns_config.json` — e.g. add `"CELL"` to
`idmz_industrial_zones` to also catch hosts that bridge enterprise and the
plant-floor cell/area zones.

> Note: if the capture shows no Enterprise↔Industrial traffic (the enterprise
> segment is isolated), there are no straddlers and `IDMZ-SW` is not drawn.

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

**In cv_converter:** network gear that Cyber Vision actually observed (Router / Firewall stencil) is **green**; the CPwE infrastructure the script synthesises to complete the topology (ENT-Core, IDMZ-FW, IND-Core, CELLSW-*) is **gray**; real servers / controllers / OT assets are **red**; real client PCs / workstations are **yellow**. This makes inferred topology immediately distinguishable from assets Cyber Vision actually observed.

## Known limitations (v1)

- **L1 is logical, not physical** — real cabling/redundancy is not in CV; the synthesized backbone is a representative CPwE topology.
- **VLAN IDs from CV are sparse**, so L2 segmentation is derived from zone/group, not real VLAN config.
- **Duplicate IPs are kept** — OT cells legitimately reuse the same RFC1918 address, so every asset is assigned its real IP (Network Sketcher accepts duplicate IPs across different devices). Only an SVI gateway IP that would repeat on the *same* L3 device is skipped.

## `Max. bandwidth(Mbps)` — important caveat

The `Max. bandwidth(Mbps)` column in `gen_flow_list.csv` uses the **same formula as
`sna_converter`**: `transferBytes * 8 / duration(seconds) / 1e6`, taking the **max**
per `(src, dst, protocol, service)` flow, formatted identically.

The critical difference is the **duration source**. SNA/NetFlow records a per-session
`activeDuration` (seconds–minutes). Cyber Vision has no such field, so the converter
uses **`Last Activity − Creation Time`** — but in CV those timestamps span the **entire
observation window** (often months or years). The reported value is therefore the
**long-term average rate over the whole capture, not a peak/session throughput**, and
will look very small (often ≈ 0). The *method* matches SNA; the *scale* does not,
because of what CV's timestamps mean.

To get meaningful bandwidth from Cyber Vision:
- **Average over a real interval** — export the activity list scoped to a short, recent
  time window (CV preset + time filter); then `Bytes / window_seconds` is valid.
- **Peak / max bandwidth** — poll the CV REST API at a fixed cadence and delta the
  cumulative byte counters (`Δbytes·8 / Δt`), taking the max; this cannot be derived
  from a single static export.
- **Highest accuracy** — pair CV (asset/zone context) with NetFlow/IPFIX → Cisco Secure
  Network Analytics, which carries true per-flow `activeDuration` (the `sna_converter`
  input).

## Author

Yusuke Ogawa - Architect, Cisco | CCIE#17583
