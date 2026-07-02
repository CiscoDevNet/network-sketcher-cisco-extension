# nd_converter — Cisco Nexus Dashboard (NDFC) to Network Sketcher Command Converter

Convert a [Cisco Nexus Dashboard](https://www.cisco.com/site/us/en/products/networking/cloud-networking/nexus-dashboard/index.html)
**Fabric Controller (NDFC)** NX-OS VXLAN EVPN fabric into ready-to-run
[Network Sketcher](https://github.com/cisco-open/network-sketcher) command
scripts. The fabric model is pulled from Nexus Dashboard over its **read-only
REST API** (`fetch_from_nd.py`) — or from a previously downloaded export — and
converted **entirely offline** into two diagrams.

> Validated end-to-end against a live **Nexus Dashboard 4.1.1** (NDFC / Fabric
> Controller) and the Network Sketcher engine: both modes import cleanly and
> every L1/L2/L3 diagram artifact generates correctly.

---

## Why two diagrams: underlay vs. overlay

**NX-OS VXLAN EVPN deliberately decouples the physical fabric from the logical
overlay** (the leaf/spine fabric forwards everything as VXLAN; tenant policy —
VRFs and Networks — is an overlay on top). They answer different questions and
do **not** line up 1:1, so this tool produces **two independent diagrams**
(selectable with `--mode`) instead of forcing them into one misleading picture:

| | **Underlay** (`--mode underlay`) | **Overlay** (`--mode overlay`) |
|---|---|---|
| Question it answers | *How is the fabric physically built/cabled?* | *What tenants/segments run on it?* |
| Contents | leaf / spine / border / border-gateway switches + real L1 links | VRF → Network (L2VNI) + anycast gateway SVIs |
| Layer | L1 (physical) | L2 / L3 (logical) |
| Output | `*_nd_underlay(L1Only).txt` | `*_nd_overlay.txt` |

> **What NDFC provides** — unlike ACI (which has no operational topology in a
> config export and must *infer* CLOS cabling), NDFC returns the **real observed
> topology**: every switch's role/model/version/mgmt-IP and the actual
> `control/links` cabling. The underlay is therefore *observed*, not inferred.

---

## Overview

| Item | Detail |
|------|--------|
| **Input** | NDFC model via the read-only REST API (`fetch_from_nd` → `nd_export.json`), **or** a previously saved combined JSON / directory of `*.json` / `.tar.gz` (auto-detected) |
| **Output** | Two NS command scripts (`*_nd_underlay(L1Only).txt`, `*_nd_overlay.txt`) + a `[Flow_List]` CSV, debug models, and audit reports |
| **Dependencies** | Python 3.10+ standard library only — no pip packages |
| **ND connectivity** | `fetch_from_nd` uses the **read-only** REST API; `convert` is **fully offline** (local file I/O only) |

## Quick Start

```bash
# 1. No install needed (stdlib only)
pip install -r requirements.txt          # no-op

# 2. Fetch the model from Nexus Dashboard over the read-only REST API
#    (password via the ND_PASSWORD env var)
ND_PASSWORD=... python -m nd_converter.src.fetch_from_nd \
    --host nd.example.com --user admin \
    --out nd_converter/Input_data/nd_export.json

# 3. Convert offline into both diagrams (no ND connection)
python -m nd_converter.src.convert \
    -i nd_converter/Input_data/nd_export.json \
    -m both \
    -o nd_converter/Output_data/ns_commands.txt \
    -c nd_converter/nd_to_ns_config.json
```

Steps 2 and 3 are **independent**: fetch once, convert as many times as you like
(different `--mode` / config) with no further ND access.

## Modes (`--mode`, default `both`)

| `--mode` | What it draws | Built from |
|----------|---------------|-----------|
| `underlay` | Physical fabric: leaf / spine / border / border-gateway switches + their **real observed** L1 links. Nodes carry real role / model / NX-OS version / mgmt-IP / serial. External neighbours (ISN / edge / core) become light-blue observed waypoints | `switchesByFabric`, `control/links` |
| `overlay` | Logical VXLAN EVPN: per-VRF gateway (anycast-GW SVIs) → Networks (L2VNIs), each Network bound to its VLAN segment. All overlay devices are **light purple** | `top-down/.../vrfs`, `top-down/.../networks` |
| `both` | `underlay` + `overlay` (default) | — |

## Output files

| File | Mode | Description |
|------|------|-------------|
| `<stem>_nd_underlay(L1Only).txt` | underlay | NS CLI commands — fabric L1 only (no L2/L3) |
| `<stem>_nd_overlay.txt` | overlay | NS CLI commands (Phase 1–6) for the VXLAN EVPN overlay |
| `gen_flow_list.csv` | overlay | `[Flow_List]` paste sheet — intra-VRF reachability (opt-in, see below) |
| `ns_model_underlay.json` / `ns_model_overlay.json` | each | Intermediate topology model (debug) |
| `nd_inventory.csv` | underlay | Switch inventory + stencil mapping (audit) |
| `nd_underlay_report.md` / `nd_overlay_report.md` | each | Counts + accuracy caveats (incl. every inferred element) |

---

## NDFC → Network Sketcher mapping

### Underlay
| NDFC | NS |
|------|-----|
| `switchRoleEnum` `Leaf` / `Spine` / `SuperSpine` | `L3Switch` device (green) |
| `Border` / `BorderSpine` | `L3Switch`, labelled "Border" |
| `BorderGateway` / `BorderGatewaySpine` / `…SuperSpine` | `L3Switch`, labelled "Border Gateway / VXLAN Multi-Site" |
| `EdgeRouter` / `CoreRouter` | `Router` |
| `model` / `release` / `ipAddress` / `serialNumber` | per-node Model, OS (NX-OS version), mgmt IP + serial (Attribute-D) |
| `control/links` `ethisl` | **observed** intra-fabric cabling (leaf↔spine, vPC peer-link) |
| `control/links` `lan_neighbor_link` | adjacency to a switch outside the inventory → light-blue **observed external waypoint** |
| `vpcpair` / switch `vpcDomain` (optional) | vPC-pair annotation in Attribute-D |
| interface `interface/detail` (optional) | real per-port Speed/Duplex/Media |

### Overlay
| NDFC | NS |
|------|-----|
| **Fabric** | an **area** (one per fabric) |
| **VRF** (`vrfName`, L3VNI `vrfId`) | a synthesised **gateway device** `VRF-GW:<fabric>-<vrf>` + an `l3_instance` (VRF) tag on its SVIs |
| **Network** (`networkName`, L2VNI `networkId`) | **one shared L2 segment** (`Vlan <id>`) **and** an NS device `NET:<network>` bound to it (the L2VNI / EPG-equivalent) |
| **Network gateway** (`gatewayIpAddress` / v6) | an **SVI** `Vlan <id>` on the VRF gateway + the subnet IP + `l3_instance` = VRF (the distributed anycast gateway) |
| **L2-only Network** (no VRF) | a floating `NET:<network>` segment device (no gateway/SVI) |
| **Endpoint Locator host** (optional) | a host device `EP_<network>_<n>` under its Network (host side = L3 IP /32·/128, network side = BD VLAN) |

> [!TIP]
> Every overlay device name starts with a **type prefix** — `VRF-GW:`, `NET:`,
> `EP_` — the same convention as `aci_converter` (`VRF-GW:`, `EPG:`, `SRV_`,
> `PC_`), so a device's role is legible from its name alone in both converters.
> `VRF-GW:DevNet_VxLAN_Fabric-RED_VRF` is the anycast gateway for VRF `RED_VRF`
> in fabric `DevNet_VxLAN_Fabric`; `NET:RED_Web` is the Network (L2VNI) segment
> device named `RED_Web`. Endpoints keep the pre-existing `EP_<network>_<n>`
> form (no colon), matching ACI's `SRV_` / `PC_` endpoint naming — NX-OS VXLAN
> EVPN has no server/client distinction (no ACI-style contracts), so there is
> only one endpoint kind. An overlay Network's device is the rough equivalent
> of ACI's EPG, hence the naming parallel — a VRF gateway or L3Out is never a
> `NET:`/`EPG:` device (and vice versa), so the prefix also disambiguates
> object type at a glance.

The `vlanId` for a Network comes from its `networkTemplateConfig`; a Network
with none is assigned a synthetic id from `vlan_base` (config, default `101`).
Overlay links use the pseudo port label `Dummy N` (they encode VRF/VLAN
membership, not cabling) and their Speed/Duplex/Port-Type are `Unknown`.

---

## ⚠️ Concepts that can NOT be fully represented

> [!IMPORTANT]
> Network Sketcher is a VLAN/port diagram tool; NX-OS VXLAN EVPN is a VXLAN
> overlay fabric. The overlay is a **faithful approximation, not a 1:1 model**:
>
> - **Network ≠ VLAN.** A Network is an L2VNI forwarded by VXLAN VNID; the
>   overlay collapses it to one representative `Vlan <id>` (exact for the common
>   1 Network = 1 VLAN design).
> - **The VRF gateway is one node, but the real anycast gateway is distributed**
>   across every leaf where the VRF is deployed. A single `VRF-GW:<fabric>-<vrf>`
>   is drawn for readability.
> - **No contracts.** VXLAN EVPN gives open any-to-any L3 reachability within a
>   VRF (no ACI-style `vzBrCP`), so no per-pair policy is drawn unless
>   `emit_intra_vrf_flows` is enabled (then one row per ordered Network pair
>   that shares a VRF — `O(n²)`, off by default).
> - **Underlay links are de-duplicated per port.** NDFC neighbour discovery can
>   report several adjacencies on one physical port; NS allows one cable per
>   port, so extras are dropped (fabric ISLs win over neighbour-only links).
>   Every dropped/inferred element is listed under `INFERRED` in the report.
>
> **Always validate against Nexus Dashboard before relying on the diagram.**

## Device color conventions

The generated `rename attribute_bulk` command writes a coloured cell into the
**Default** column of the Network Sketcher Attribute sheet —
`\"['DEVICE',[R,G,B]]\"` (WayPoints keep their token: `\"['WayPoint',[R,G,B]]\"`)
— so every device is colour-coded by role in the Device Table. The base palette
is **shared across every converter in this repo**:

| Colour | RGB | Meaning | Used in nd_converter |
|--------|-----|---------|----------------------|
| 🟩 Light green | `[235, 241, 222]` | **Observed network gear** — real router / L3 switch / switch / firewall / WLC / AP | ✅ underlay: leaf / spine / border / BGW switches |
| 🟥 Light red | `[255, 204, 204]` | **Server-role endpoint** — server / controller / internet service | base palette (nd recolours overlay hosts purple — see below) |
| 🟨 Light yellow | `[255, 255, 204]` | **Client endpoint** — PC / workstation / phone | base palette (nd recolours overlay devices purple — see below) |
| 🟪 Light purple | `[221, 204, 255]` | **Logical / overlay** — every VRF gateway, Network and endpoint host | ✅ overlay: all logical devices |
| 🟦 Light blue | `[220, 230, 242]` | **Observed WayPoint** — a WayPoint backed by a real source record. Also the fixed colour of the **Stencil Type** attribute column | ✅ underlay: external neighbour waypoints (ISN / edge / core — real switches, just outside the fabric inventory) — and (as the Stencil-Type column cell on every device) |
| ⬜ Light gray | `[200, 200, 200]` | **Inferred / synthesised** — placeholders that do not exist in the source | not currently emitted — nd_converter has no purely-synthesised device today |

Two further fixed cell colours appear in every device row (set by the shared
`ns_command_builder`, not role-based): the **Model** column is pink
`[255, 183, 219]` and the **OS** column is light blue `[200, 230, 255]`.

**In nd_converter:** the **underlay** colours fabric switches green; an
external neighbour reached via `lan_neighbor_link` is a real, observed switch
(just outside the fabric inventory), so it renders **light blue**, not gray.
The **overlay** colours *every* logical device (VRF gateway, Network,
endpoint host) light purple, so it is instantly distinguishable from the
physical fabric — this is why the base-palette red / yellow (server / client)
are not emitted as Default-cell colours here even though endpoint hosts use
the `Server` stencil type.

## Accuracy caveats

- **Underlay accuracy depends on what Nexus Dashboard returns.** Switch
  role/model/NX-OS version/mgmt-IP/serial and the `control/links` cabling are
  all **observed** from the live fabric — nothing is inferred here (unlike
  `aci_converter`, which must infer ACI's CLOS mesh from a config-only
  export). A link to a switch outside the inventory is still a real, observed
  switch — it becomes a light-blue observed external waypoint, not a gray
  inferred one.
- **The overlay is a synthesized logical layout, not cabling.** The VRF
  gateway device and its SVIs do not exist as such in NDFC — they are
  synthesized to represent the distributed anycast gateway. Every Network
  collapses to one representative `Vlan <id>` segment (exact for the common
  1 Network = 1 VLAN design — see "Concepts that can NOT be fully
  represented" above).
- **Endpoint Locator hosts are optional.** Not every fabric has EPL enabled,
  so hosts may not appear under every Network — see "Readable Nexus Dashboard
  data sources" below for what each source contributes.
- **Underlay link de-duplication can drop real adjacencies.** NDFC neighbour
  discovery can report several adjacencies on one physical port; NS allows
  one cable per port, so extras are dropped (fabric ISLs win over
  neighbour-only links). Every dropped/inferred element is listed under
  `INFERRED` in the report.

## `fetch_from_nd.py` — pulling the model over the REST API

The converter reads local files, but `fetch_from_nd.py` grabs the model straight
from a reachable Nexus Dashboard and writes a `convert`-ready combined JSON.
Retrieval is **read-only** — it never modifies the fabric. It logs in
(`POST /login` → JWT), then for every fabric (or those named with `--fabric`)
pulls the switch inventory, the observed links, and the VRF / Network overlay.
Credentials come from a CLI arg or the `ND_PASSWORD` env var (never hard-coded);
ND self-signed certs are accepted by default (`--verify-tls` to enforce).

Each query is independent and a failed one is **skipped, not fatal** — a partial
fetch still produces the best diagram the data allows.

Optional richer sources are controlled by flags: vPC pairs + Multi-Site
associations are pulled by default (cheap; `--no-vpc` to skip), Endpoint Locator
hosts are attempted by default (`--no-endpoints` to skip), and per-switch
interface detail is opt-in with `--with-interfaces` (one call per switch).

### Combined JSON format (also the offline input)

```json
{
  "fabrics":  [ { "fabricName": "...", "fabricTechnology": "...", ... } ],
  "switches": { "<fabric>": [ <switch>, ... ] },
  "links":    { "<fabric>": [ <link>,   ... ] },
  "vrfs":     { "<fabric>": [ <vrf>,    ... ] },
  "networks": { "<fabric>": [ <network>, ... ] }
}
```

`convert.py` also accepts a **directory** of per-endpoint `*.json` dumps
(filenames hint the collection + fabric, e.g. `switches_<fabric>.json`) or a
`.tar.gz` of either.

## Readable Nexus Dashboard data sources

Nexus Dashboard exposes far more than the core fabric model. The converter
consumes the **core** sources always and the **optional richer** sources
whenever they are present in the export — a fabric that has them gets a richer
diagram, and one that does not is unaffected (every optional source degrades to
"not shown" with a `NOTE` in the report).

| Source (NDFC REST) | Diagram use | Status |
|---|---|---|
| `control/fabrics` | fabrics → areas | **core** ✅ verified |
| `…/inventory/switchesByFabric` | switches → nodes (role/model/version/IP) | **core** ✅ verified |
| `control/links/fabrics/{F}` | observed L1 cabling | **core** ✅ verified |
| `top-down/…/vrfs` + `/networks` | VRF gateways + Network segments + SVIs | **core** ✅ verified |
| `vpcpair` (+ switch `vpcDomain`) | underlay vPC-pair annotation | optional ✅ verified |
| interface `interface/detail` | real port Speed/Duplex/Media (`--with-interfaces`) | optional ✅ verified |
| Endpoint Locator hosts | overlay: real hosts under each Network | optional ⚠️ supported where EPL is enabled |
| `control/fabrics/msd/fabric-associations` | Multi-Site member-fabric grouping | optional ✅ verified |
| Inter-fabric / `lan_neighbor_link` to non-inventory switches | external waypoints (ISN / edge / core) | **core** ✅ verified |

## Running the output in Network Sketcher

Each `*.txt` is a plain-text script (one command per line; `#` lines are phase
comments) already ordered Phase 1→6. Install
[Network Sketcher](https://github.com/cisco-open/network-sketcher), create an
empty master, run the command lines against it, and export the diagram. Paste
`gen_flow_list.csv` into the master's `[Flow_List]` sheet if you enabled
intra-VRF flows.

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
- **Content marker:** the `underlay` L2 SVG must contain a switch name (a
  spine/leaf, e.g. `site1-leaf1`); the `overlay` L2 SVG must contain a
  `VRF-GW:` or `NET:` prefixed device name. A mode whose diagram shows the
  *other* mode's names was built from a dirty workspace.
- **Cross-mode diff:** the `underlay` and `overlay` `[L1L2L3_DIAGRAM]` HTML files
  must **not** be byte-identical.
- **Freshness:** each artifact's modification time must be newer than the moment
  you started this mode's build.

If any check fails, the workspace was dirty — redo this mode in a brand-new
empty workspace (Rule 1).

## Configuration (`nd_to_ns_config.json`)

Every key is `{value, description, sample}`; only `value` is read. Key options:
`fabric_include` (limit to named fabrics), `switch_naming`
(`name` | `name_ip` | `ip` | `serial`), `include_external_neighbors`,
`vlan_base` (first synthetic Network VLAN id), `include_endpoints` +
`max_endpoints_per_network` (Endpoint Locator hosts), and `emit_intra_vrf_flows`.

## Author

Yusuke Ogawa - Architect, Cisco | CCIE#17583

## License

This tool is part of the **Network Sketcher Cisco Extension** project, licensed
under the [Apache License 2.0](../LICENSE). See the [NOTICE](../NOTICE) file for
copyright and third-party attributions.
