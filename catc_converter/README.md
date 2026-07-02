# catc_converter — Cisco Catalyst Center (DNA Center) to Network Sketcher Command Converter

Convert a [Cisco Catalyst Center](https://www.cisco.com/site/us/en/products/networking/wireless/catalyst-center/index.html)
(formerly DNA Center) campus / **SD-Access** deployment into ready-to-run
[Network Sketcher](https://github.com/cisco-open/network-sketcher) command
scripts. The model is pulled from Catalyst Center over its **read-only Intent
REST API** (`fetch_from_catc.py`) — or from a previously downloaded export — and
converted **entirely offline** into two diagrams.

---

## Why two diagrams: underlay vs. overlay

**SD-Access deliberately decouples the physical campus from the logical fabric
overlay** (the core/distribution/access switches forward everything as VXLAN;
tenant policy — Virtual Networks and anycast gateways — is an overlay on top).
They answer different questions and do **not** line up 1:1, so this tool produces
**two independent diagrams** (selectable with `--mode`) instead of forcing them
into one misleading picture:

| | **Underlay** (`--mode underlay`) | **Overlay** (`--mode overlay`) |
|---|---|---|
| Question it answers | *How is the campus physically built/cabled?* | *What VNs/segments run on it?* |
| Contents | core / distribution / access switches, routers, WLCs, APs + real L1 links | VN gateway → anycast-gateway segments (Vlan + SVI) + border hand-off |
| Layer | L1 (physical) | L2 / L3 (logical) |
| Output | `*_catc_underlay(L1Only).txt` | `*_catc_overlay.txt` |

> **What Catalyst Center provides** — like NDFC and unlike ACI (which has no
> operational topology in a config export and must *infer* cabling), Catalyst
> Center returns the **real observed topology**: every device's
> role/platform/IOS-XE version/mgmt-IP and the actual `physical-topology`
> cabling. The underlay is therefore *observed*, not inferred.

---

## Overview

| Item | Detail |
|------|--------|
| **Input** | Catalyst Center model via the read-only Intent REST API (`fetch_from_catc` → `catc_export.json`), **or** a previously saved combined JSON / directory of `*.json` / `.tar.gz` (auto-detected) |
| **Output** | Two NS command scripts (`*_catc_underlay(L1Only).txt`, `*_catc_overlay.txt`) + a `[Flow_List]` CSV, debug models, and audit reports |
| **Dependencies** | Python 3.10+ standard library only — no pip packages |
| **Connectivity** | `fetch_from_catc` uses the **read-only** REST API; `convert` is **fully offline** (local file I/O only) |

## Quick Start

```bash
# 1. No install needed (stdlib only)
pip install -r requirements.txt          # no-op

# 2. Fetch the model from Catalyst Center over the read-only REST API
#    (password via the CATC_PASSWORD env var)
CATC_PASSWORD=... python -m catc_converter.src.fetch_from_catc \
    --host catc.example.com --user admin \
    --out catc_converter/Input_data/catc_export.json

# 3. Convert offline into both diagrams (no Catalyst Center connection)
python -m catc_converter.src.convert \
    -i catc_converter/Input_data/catc_export.json \
    -m both \
    -o catc_converter/Output_data/ns_commands.txt \
    -c catc_converter/catc_to_ns_config.json
```

Steps 2 and 3 are **independent**: fetch once, convert as many times as you like
(different `--mode` / config) with no further Catalyst Center access.

## Modes (`--mode`, default `both`)

| `--mode` | What it draws | Built from |
|----------|---------------|-----------|
| `underlay` | Physical campus: core / distribution / access switches, routers, WLCs, APs + their **real observed** L1 links. Nodes carry real role / platform / IOS-XE version / mgmt-IP / serial. Unmanaged neighbours become light-blue observed waypoints | `network-device`, `topology/physical-topology` |
| `overlay` | Logical SD-Access: per-VN gateway (anycast-GW SVIs) → anycast-gateway segments, each bound to its VLAN. Border nodes add an L3 hand-off cloud. All overlay devices are **light purple** | `sda/fabricSites`, `sda/layer3VirtualNetworks`, `sda/anycastGateways`, `sda/fabricDevices` |
| `both` | `underlay` + `overlay` (default) | — |

## Output files

| File | Mode | Description |
|------|------|-------------|
| `<stem>_catc_underlay(L1Only).txt` | underlay | NS CLI commands — campus L1 only (no L2/L3) |
| `<stem>_catc_overlay.txt` | overlay | NS CLI commands (Phase 1–6) for the SD-Access overlay |
| `gen_flow_list.csv` | overlay | `[Flow_List]` paste sheet — SGT flows (opt-in; empty by default, see below) |
| `ns_model_underlay.json` / `ns_model_overlay.json` | each | Intermediate topology model (debug) |
| `catc_inventory.csv` | underlay | Device inventory + stencil mapping (audit) |
| `catc_underlay_report.md` / `catc_overlay_report.md` | each | Counts + accuracy caveats (incl. every inferred element) |

---

## Catalyst Center → Network Sketcher mapping

### Underlay
| Catalyst Center | NS |
|------|-----|
| `role` `CORE` / `DISTRIBUTION` / `BORDER` / `BORDER ROUTER` | `L3Switch` device (green) |
| `role` `ACCESS` (edge) | `Switch` device (green) |
| `family` `Routers` (role `router`) | `Router` |
| `family` `Wireless Controller` (role `wlc`) | `WLC` (Catalyst 9800) |
| `family` `Unified AP` (role `ap`) | `AP` (Catalyst Access Point) |
| `platformId` / `softwareType` + `softwareVersion` / `managementIpAddress` / `serialNumber` | per-node Model, OS (e.g. "IOS-XE 17.18.2"), mgmt IP + serial (Attribute-D) |
| `physical-topology.links` (`startPortName` / `endPortName`) | **observed** L1 cabling (topology node id == network-device id) |
| topology node that is NOT a managed device | light-blue **observed external waypoint** (unmanaged neighbour) |
| interface `interface/network-device/{id}` (optional) | real per-port Speed/Duplex/Media |

### Overlay
| Catalyst Center | NS |
|------|-----|
| **fabric site** (`sda/fabricSites`) | an **area** (named after the site-hierarchy leaf, e.g. `CML-Lab`) |
| **Layer3 Virtual Network** (`virtualNetworkName`) | a synthesised **gateway device** `<area>-<VN>-GW` + an `l3_instance` (VRF = VN name) on its SVIs |
| **anycast gateway** (`vlanId`, `ipPoolName`, `vlanName`) | an **L2 segment** (`Vlan <id>`) **and** an **SVI** `Vlan <id>` on the VN gateway, labelled with the IP-pool name |
| **fabricDevices** `deviceRoles` (`CONTROL_PLANE` / `BORDER` / `EDGE`) | Attribute-D annotation on the VN gateway; a `BORDER` node adds an external cloud linked to the gateways |
| **client** (optional, `clients` API) | a host device `EP_<segment>_<n>` under a segment in its site (host side = L3 IP /32·/128, segment side = VLAN) |

The `vlanId` for a segment comes from its `anycastGateway` object; one with none
is assigned a synthetic id from `vlan_base` (config, default `101`). Overlay
links use the pseudo port label `Dummy N` (they encode VN/VLAN membership, not
cabling) and their Speed/Duplex/Port-Type are `Unknown`.

---

## ⚠️ Concepts that can NOT be fully represented

> [!IMPORTANT]
> Network Sketcher is a VLAN/port diagram tool; SD-Access is a VXLAN fabric
> overlay. The overlay is a **faithful approximation, not a 1:1 model**:
>
> - **The VN gateway is one node, but the real anycast gateway is distributed**
>   across every fabric edge node where the VN is provisioned. A single
>   `<area>-<VN>-GW` is drawn per fabric site for readability.
> - **No concrete subnet on the SVI.** The `anycastGateway` object carries only
>   the reserved IP-pool *name* (`ipPoolName`) + VLAN, not the CIDR, so the SVI
>   is labelled with the pool name and VLAN — no IP/mask is assigned. (Add a
>   reserved-pool fetch to enrich this.)
> - **No contracts.** SD-Access gives open any-to-any reachability within a VN;
>   inter-group segmentation is SGT / group-policy based (a separate API
>   surface), so no per-pair policy is drawn unless `emit_sgt_flows` is enabled
>   (SGT flow emission is **not implemented yet** — it currently emits no rows).
> - **Client placement is per-site, not per-VLAN.** A client object carries a
>   site + connected device but not enough to derive its exact segment, so it is
>   attached to the first segment of its site.
> - **Underlay links are de-duplicated per port.** NS allows one cable per
>   physical port, so extra topology adjacencies on the same port are dropped.
>   Every dropped/inferred element is listed under `INFERRED` in the report.
>
> **Always validate against Catalyst Center before relying on the diagram.**

## Device color conventions

The generated `rename attribute_bulk` command writes a coloured cell into the
**Default** column of the Network Sketcher Attribute sheet —
`\"['DEVICE',[R,G,B]]\"` (WayPoints keep their token: `\"['WayPoint',[R,G,B]]\"`)
— so every device is colour-coded by role in the Device Table. The base palette
is **shared across every converter in this repo**:

| Colour | RGB | Meaning | Used in catc_converter |
|--------|-----|---------|------------------------|
| 🟩 Light green | `[235, 241, 222]` | **Observed network gear** — real router / L3 switch / switch / WLC / AP | ✅ underlay: campus switches / routers / WLCs / APs |
| 🟥 Light red | `[255, 204, 204]` | **Server-role endpoint** | base palette (catc recolours overlay hosts purple — see below) |
| 🟨 Light yellow | `[255, 255, 204]` | **Client endpoint** — PC / workstation / phone | base palette (catc recolours overlay devices purple — see below) |
| 🟪 Light purple | `[221, 204, 255]` | **Logical / overlay** — every VN gateway, segment, border cloud and client host | ✅ overlay: all logical devices |
| 🟦 Light blue | `[220, 230, 242]` | **Observed WayPoint** — a WayPoint backed by a real source record. Also the fixed colour of the **Stencil Type** attribute column | ✅ underlay: unmanaged neighbour waypoints (real topology nodes, just not Catalyst-Center-managed) — and (as the Stencil-Type column cell on every device) |
| ⬜ Light gray | `[200, 200, 200]` | **Inferred / synthesised** — placeholders that do not exist in the source | ✅ underlay: the inferred shared-L3-segment device + dummy wireless clients |

Two further fixed cell colours appear in every device row (set by the shared
`ns_command_builder`, not role-based): the **Model** column is pink
`[255, 183, 219]` and the **OS** column is light blue `[200, 230, 255]`.

**In catc_converter:** the **underlay** colours campus devices green; an
unmanaged neighbour reached via the physical-topology API is a real, observed
node (just not Catalyst-Center-managed), so it renders **light blue**, while
devices the converter itself invents (the inferred shared-L3-segment node,
dummy wireless clients) render **gray**. The **overlay** colours *every*
logical device (VN gateway, segment, border cloud, client host) light purple,
so it is instantly distinguishable from the physical campus.

## Accuracy caveats

- **Underlay accuracy depends on what Catalyst Center returns.** Device
  role/platform/IOS-XE version/mgmt-IP/serial and the `physical-topology`
  cabling are all **observed** from the live fabric — nothing is inferred
  here (unlike `aci_converter`, which must infer ACI's CLOS mesh from a
  config-only export). A topology node that maps to no managed device is
  still a real, observed node — it becomes a light-blue observed external
  waypoint, not a gray inferred one.
- **The overlay is a synthesized logical layout, not cabling.** The VN
  gateway device, its SVIs, and the border hand-off cloud do not exist as
  such in Catalyst Center — they are synthesized to represent VN /
  anycast-gateway policy. Overlay link ports always use the pseudo label
  `Dummy N`; every synthesized element is listed under `INFERRED` / `MODEL`
  in `catc_overlay_report.md`.
- **Anycast-gateway VLAN ids are sometimes synthetic.** A gateway with no
  `vlanId` gets one assigned from `vlan_base` (config, default `101`) rather
  than a real value.
- **Client placement is approximate** — see "Concepts that can NOT be fully
  represented" above (a client is attached to the first segment of its site,
  not necessarily its exact VLAN).

## `fetch_from_catc.py` — pulling the model over the REST API

The converter reads local files, but `fetch_from_catc.py` grabs the model
straight from a reachable Catalyst Center and writes a `convert`-ready combined
JSON. Retrieval is **read-only** — it never modifies the fabric. It authenticates
(`POST /dna/system/api/v1/auth/token` with **HTTP Basic** auth → `{"Token":
"<jwt>"}`), then sends that token on every subsequent GET in the
**`X-Auth-Token`** header (not a Cookie / Bearer). Credentials come from a CLI
arg or the `CATC_PASSWORD` env var (never hard-coded); Catalyst Center
self-signed certs are accepted by default (`--verify-tls` to enforce).

Network-device and interface collections are **paged** with
`?offset=N&limit=500` (offset is 1-based; the fetcher loops until a page returns
fewer than the limit). `fabricDevices` and `anycastGateways` REQUIRE a `fabricId`
query param, so the fetcher first reads `fabricSites` and then loops its ids.

Each query is independent and a failed one is **skipped, not fatal** — a partial
fetch still produces the best diagram the data allows. Clients
(`--with-endpoints`) and per-device interface detail (`--with-interfaces`, one
call per device) are opt-in.

### Combined JSON format (also the offline input)

```json
{
  "_meta": {"source": "Cisco Catalyst Center / DNA Center", "host": "..."},
  "devices":    [ <network-device>, ... ],
  "interfaces": { "<deviceId>": [ <interface>, ... ] },
  "physicalTopology": {"nodes": [...], "links": [...]},
  "sites":       [ <site>, ... ],
  "fabricSites":  [ <fabricSite>, ... ],
  "fabricDevices":[ <fabricDevice>, ... ],
  "layer3VirtualNetworks": [ <l3vn>, ... ],
  "anycastGateways":       [ <anycastGateway>, ... ],
  "clients":     [ <client>, ... ]
}
```

`convert.py` also accepts a **directory** of per-endpoint `*.json` dumps
(filenames hint the collection, e.g. `devices.json`, `fabric_devices.json`) or a
`.tar.gz` of either.

## Running the output in Network Sketcher

Each `*.txt` is a plain-text script (one command per line; `#` lines are phase
comments) already ordered Phase 1→6. Install
[Network Sketcher](https://github.com/cisco-open/network-sketcher), create an
empty master, run the command lines against it, and export the diagram. Paste
`gen_flow_list.csv` into the master's `[Flow_List]` sheet if you enabled SGT
flows.

### AI-agent runbook — generate the diagrams without mistakes

> [!IMPORTANT]
> The diagram build runs in the **Network Sketcher engine** (e.g. the
> `network-sketcher` MCP), NOT in this converter — the converter only emits the
> `*.txt` command scripts. The engine names **every** artifact after its master
> (always `[MASTER]no_data.nsm`), so generating a second mode in a workspace that
> still holds the first mode's diagrams makes the build **reuse the stale
> diagrams** (the device table refreshes, but the SVG/HTML do not). Follow the
> rules below to avoid it.

**Rule 1 — one mode = one clean, dedicated workspace.** Never reuse a workspace
across modes or runs. The workspace must be under your **home directory** (the
engine rejects paths outside it).

**Rule 2 — ordered steps, per mode** (`underlay`, then `overlay`):
1. Use a **fresh empty workspace directory for this mode** (e.g.
   `~/ns_ws_underlay`, `~/ns_ws_overlay`). If reusing a directory, first delete
   every prior artifact in it: `[MASTER]*.nsm`, `[L1_DIAGRAM]*`, `[L2_DIAGRAM]*`,
   `[L3_DIAGRAM]*`, `[L1L2L3_DIAGRAM]*`, `[DEVICE_TABLE]*`, `[AI_Context]*`.
2. `create_empty_master` → `[MASTER]no_data.nsm`.
3. `run_commands` with this mode's `*.txt`. Pass the commands as a
   **newline-delimited single string with the `#` comment lines removed** — NOT
   a JSON array (an array is parsed as one bad verb and fails). Expect
   `N OK, 0 FAIL`; if any command FAILs, stop and fix before continuing.
4. `build_default_outputs` → expect `Summary: 6/6 succeeded.`
5. **Immediately move/rename** the six artifacts out of the workspace, tagged by
   mode (e.g. `..._underlay.svg` / `..._overlay.html`), before touching the next
   mode.

**Rule 3 — verify before trusting the result (do all three):**
- **Content marker:** the `underlay` L2 SVG must contain a campus device name (a
  core/access switch); the `overlay` L2 SVG must contain an overlay name (a VN
  gateway `*-GW` or a segment). A mode whose diagram shows the *other* mode's
  names was built from a dirty workspace.
- **Cross-mode diff:** the `underlay` and `overlay` `[L1L2L3_DIAGRAM]` HTML files
  must **not** be byte-identical.
- **Freshness:** each artifact's modification time must be newer than the moment
  you started this mode's build.

If any check fails, the workspace was dirty — redo this mode in a brand-new
empty workspace (Rule 1).

## Configuration (`catc_to_ns_config.json`)

Every key is `{value, description, sample}`; only `value` is read. Key options:
`site_include` (limit to named fabric-site areas), `device_naming`
(`hostname` | `hostname_ip` | `ip`), `strip_domain_suffix` (drop the dCloud /
site domain from hostnames), `include_external_neighbors`, `vlan_base` (first
synthetic anycast-gateway VLAN id), `include_endpoints` +
`max_endpoints_per_vn` (clients), and `emit_sgt_flows`.

## Author

Yusuke Ogawa - Architect, Cisco | CCIE#17583

## License

This tool is part of the **Network Sketcher Cisco Extension** project, licensed
under the [Apache License 2.0](../LICENSE). See the [NOTICE](../NOTICE) file for
copyright and third-party attributions.
