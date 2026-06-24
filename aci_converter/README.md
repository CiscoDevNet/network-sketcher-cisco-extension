# aci_converter — Cisco ACI to Network Sketcher Command Converter

Convert a [Cisco ACI](https://www.cisco.com/c/en/us/solutions/data-center-virtualization/application-centric-infrastructure/index.html)
fabric into ready-to-run
[Network Sketcher](https://github.com/cisco-open/network-sketcher) command
scripts. The fabric model is pulled from the APIC over its **read-only REST API**
(`fetch_from_apic.py`) — or from a manually downloaded APIC Configuration Export
— and converted **entirely offline** into two diagrams.

---

## Why two diagrams: underlay vs. overlay

**ACI deliberately decouples the physical fabric from the logical policy** (the
spine/leaf fabric forwards everything as VXLAN; tenant policy is an overlay on
top). They answer different questions and do **not** line up 1:1, so this tool
produces **two independent diagrams** (selectable with `--mode`) instead of
forcing them into one misleading picture:

| | **Underlay** (`--mode underlay`) | **Overlay** (`--mode overlay`) |
|---|---|---|
| Question it answers | *How is the fabric physically built/cabled?* | *What tenants/segments/policies run on it?* |
| Contents | spine / leaf / border-leaf / APIC + L1 links | Tenant → VRF → Bridge Domain → EPG + endpoints + contracts |
| Layer | L1 (physical) | L2 / L3 (logical) |
| Output | `*_aci_underlay(L1Only).txt` | `*_aci_overlay.txt` |

Trying to draw both as one graph is misleading because, e.g., an EPG's endpoints
are spread across many leafs, a contract can join EPGs in *different* BDs/VRFs
(L3), and the gateway is distributed across every leaf. Keeping them separate
lets each diagram stay accurate within its own layer. (A future fused view is
possible via static path bindings but is intentionally **not** built today.)

> **What the APIC provides** — with the default API fetch, the converter gets
> both the *policy* model (tenants, VRFs, bridge domains, EPGs, contracts,
> L3Outs, access policies) **and** operational state (`fabricNode`, `topSystem`,
> `lldpAdjEp`, `fvCEp` endpoints, vPC). A `--no-topology` fetch — or a manually
> downloaded Configuration Export — contains only the policy model; the underlay
> then infers roles + CLOS cabling and the overlay shows no endpoints.

---

## Overview

| Item | Detail |
|------|--------|
| **Input** | APIC model via the read-only REST API (`fetch_from_apic` → `apic_export.json`), **or** a manual APIC Configuration Export (`.tar.gz` / directory of `*.json` / single merged JSON — auto-detected) |
| **Output** | Two NS command scripts (`*_aci_underlay(L1Only).txt`, `*_aci_overlay.txt`) + a `[Flow_List]` CSV, debug models, and audit reports |
| **Dependencies** | Python 3.10+ standard library only — no pip packages |
| **APIC connectivity** | `fetch_from_apic` uses the **read-only** REST API; `convert` is **fully offline** (local file I/O only) |

> [!NOTE]
> The **validated** input path is the read-only REST API (`fetch_from_apic`) —
> verified end-to-end against a live APIC and the Network Sketcher engine. File
> input (a `polUni` MIT as `.tar.gz` / directory of `*.json` / single JSON) is
> parsed and works with synthetic data and the REST-API output, but a **real
> APIC GUI Configuration Export tarball has not yet been verified** end-to-end —
> its real-world layout (split files, wrappers) may need adjustment. A manual
> export is also **config-only** (equivalent to `--no-topology`: no operational
> topology, no APIC node, no real endpoints). Please report any export that does
> not parse.

## Quick Start

```bash
# 1. No install needed (stdlib only)
pip install -r requirements.txt          # no-op

# 2. Fetch the model from the APIC over the read-only REST API
#    (password via the ACI_PASSWORD env var; default also pulls operational
#     topology + endpoints — add --no-topology for a config-only pull)
ACI_PASSWORD=... python -m aci_converter.src.fetch_from_apic \
    --host apic.example.com --user admin \
    --out aci_converter/Input_data/apic_export.json

# 3. Convert offline into both diagrams (no APIC connection)
python -m aci_converter.src.convert \
    -i aci_converter/Input_data/apic_export.json \
    -m both \
    -o aci_converter/Output_data/ns_commands.txt \
    -c aci_converter/aci_to_ns_config.json
```

Steps 2 and 3 are **independent**: fetch once, convert as many times as you like
(different `--mode` / config) with no further APIC access. A Configuration Export
downloaded from the APIC GUI (Admin → Import/Export → Export Policies,
Format = JSON) can also be fed to `convert` directly, but that path is **not yet
verified against a real GUI export tarball** (see the note above).

## Modes (`--mode`, default `both`)

| `--mode` | What it draws | Built from |
|----------|---------------|-----------|
| `underlay` | Physical fabric: spine / leaf / border-leaf / APIC + L1 links (observed via LLDP, else inferred CLOS). Nodes carry real role / model / version / mgmt-IP / vPC pairing | `fabricNode` (or `fabricNodeIdentP`), `topSystem`, `lldpAdjEp`, vPC MOs |
| `overlay` | Logical policy: Tenant → VRF gateway (BD SVIs/subnets) → EPGs, each EPG's **real endpoints** beneath it; contracts as a `[Flow_List]` sheet. All overlay devices are **light purple** | `fvTenant`/`fvCtx`/`fvBD`/`fvAEPg`/`vzBrCP`/`fvCEp` |
| `both` | `underlay` + `overlay` (default) | — |

## Output files

| File | Mode | Description |
|------|------|-------------|
| `<stem>_aci_underlay(L1Only).txt` | underlay | NS CLI commands — fabric L1 only (no L2/L3) |
| `<stem>_aci_overlay.txt` | overlay | NS CLI commands (Phase 1–6) for the policy overlay |
| `gen_flow_list.csv` | overlay | `[Flow_List]` paste sheet — contract flows (consumer→provider) |
| `ns_model_underlay.json` / `ns_model_overlay.json` | each | Intermediate topology model (debug) |
| `aci_inventory.csv` | underlay | Fabric node inventory + stencil mapping (audit) |
| `aci_underlay_report.md` / `aci_overlay_report.md` | each | Counts + accuracy caveats (incl. every inferred element) |

---

## Overlay representation rules (read this before reading an overlay diagram)

The overlay maps the ACI policy hierarchy onto Network Sketcher objects. Because
NS is a VLAN/port-based drawing tool and ACI is a VXLAN policy model, several
choices are deliberate. **This section is the contract between "what you see in
NS" and "what it means in ACI".**

### 1. Hierarchy → NS objects

| ACI object | NS representation |
|------------|-------------------|
| **Tenant** (`fvTenant`) | an **area** (one per tenant) |
| **VRF** (`fvCtx`) | a synthesised **gateway device** `<tenant>-<vrf>-GW`, and an `l3_instance` (VRF) tag on its SVIs |
| **Bridge Domain** (`fvBD`) | **one shared L2 segment (a VLAN)** = the broadcast domain (see §2) |
| **BD subnet** (`fvSubnet`) | an **SVI** `Vlan <id>` on the VRF gateway + the subnet IP + `l3_instance` = VRF (this is the distributed anycast gateway) |
| **EPG** (`fvAEPg`) | a **device** named `<BD>-<AP>-<EPG>` (see §3), a *member* of its BD's L2 segment |
| **Endpoint** (`fvCEp`/`fvIp`) | a **host device** with its real IP on an L3 port (see §4) |
| **Contract** (`vzBrCP`) | **NOT a link** — a row in `gen_flow_list.csv` (see §5) |
| **L3Out** (`l3extOut`) | a gray **cloud waypoint** linked to the VRF gateway |

### 2. How a Bridge Domain is represented — and what the segment value is

- A **BD = one broadcast domain = one shared L2 segment** in NS. **All EPGs that
  belong to the same BD are bound to the same VLAN**, so they render as a single
  broadcast domain — matching ACI's default flooding (broadcast reaches every
  EPG in the BD).
- **The segment value is `Vlan <id>`**, where `<id>` is:
  - the **encap VLAN** taken from a static path binding (`fvRsPathAtt`,
    `vlan-NNN`) for that BD when one exists; otherwise
  - a **synthetic** id assigned sequentially from `vlan_base` (config, default
    `101`). Each BD is guaranteed a **distinct** id, so different BDs are always
    different broadcast domains.
- **Why `Vlan <id>` and not the BD name?** The same VLAN also carries the
  anycast-gateway **SVI** on the VRF gateway, and Network Sketcher requires an
  SVI to be a numeric `Vlan <id>` (it rejects e.g. `Vlan Web-BD`). Naming
  the segment after the BD would break the SVI↔segment gateway linkage. The BD
  name is instead surfaced in the **EPG device name** and in each EPG's
  `BD <name>` attribute.

### 3. EPG device naming — `<BD>-<AP>-<EPG>`

An EPG's true identity in ACI is **`tenant / Application-Profile / EPG`** — EPG
short names repeat across Application Profiles (and even within one BD). So the
device name includes the **BD** (broadcast-domain context) **and** the
**Application Profile** (disambiguation):

```
App-BD-Shop-App    = BD "App-BD", AppProfile "Shop",  EPG "App"
Web-BD-Shop2-Web   = BD "Web-BD", AppProfile "Shop2", EPG "Web"
```

> Without the AppProfile, the two EPGs named `Web` that share BD `Web-BD` (in App
> Profiles `Shop` and `Shop2`) would collide and get a meaningless `_2` suffix.
> `<BD>-<AP>-<EPG>` keeps every name unique and self-explanatory. The full
> `AP/EPG` is also in the device's **Model** attribute (e.g. `EPG Shop2/Web`).

### 4. Endpoints — servers are L3 hosts, clients collapse

An EPG's role is inferred from **contract direction** (provides a contract →
**server**; consumes only → **client**; otherwise by EPG-name keyword — ACI does
not label endpoints natively). Then:

| Role | NS device(s) | Name | Port |
|------|--------------|------|------|
| **Server** EPG | one device per endpoint | `SRV_<AP>-<EPG>_<seq>` | host port is **L3** with the endpoint's real IPs as host CIDRs (`/32`, `/128`) |
| **Client** EPG | all endpoints collapse into one segment device | `PC_<AP>-<EPG>_<n>` (`n` = endpoint count) | one L3 host port carrying the client IPs |

The endpoint sits in the BD broadcast domain the real way: the **host side is an
L3 port (IP)**, and the **EPG (switch) side of that link carries the BD VLAN**.
MAC / encap / vPC / attach-location are recorded in each device's Attribute-D.

### 5. Contracts are flows, not links

A contract is a **policy filter**, not L2 adjacency — and it frequently joins
EPGs in different BDs/VRFs (i.e. routed, L3). It is therefore emitted as
`gen_flow_list.csv` rows (`consumer → provider`, with protocol/port from the
filter's `vzEntry`), **never** as a topology link. Paste that CSV into the NS
master's `[Flow_List]` sheet.

### 6. Link ports are pseudo (`Dummy N`)

The overlay's links (EPG↔gateway, endpoint↔EPG, L3Out↔gateway) are **logical
relationships, not cables** — ACI has no physical interface for them. So their
port names use the pseudo label **`Dummy 0`, `Dummy 1`, …** to make clear they
are synthetic and must not be read as real fabric ports. For the same reason,
overlay **Speed / Duplex / Port Type are all set to `Unknown`** in the Device
Table. (The gateway SVI keeps a real `Vlan <id>` because it is a genuine L3
interface.)

### Worked example (one BD, multiple EPGs)

This is the bundled `Input_data/sample_export.json` (tenant `ExampleCorp`):

```
Area  "ExampleCorp"  (tenant)
  ExampleCorp-Prod-VRF-GW                 ← VRF gateway (anycast GW); SVIs Vlan110=192.0.2.1/24, Vlan120=198.51.100.1/24; VRF Prod-VRF
    Vlan110  (broadcast domain = BD "Web-BD")
      ├─ Web-BD-Shop-Web                   ← EPG (client), member of Vlan110
      │    └─ PC_Shop-Web_3  (Dummy 0 = 192.0.2.10/32, 192.0.2.11/32, 192.0.2.12/32)   ← 3 clients collapsed, L3
      └─ Web-BD-Shop2-Web                  ← EPG (client), member of Vlan110
    Vlan120  (broadcast domain = BD "App-BD")
      └─ App-BD-Shop-App                   ← EPG (server), member of Vlan120
           ├─ SRV_Shop-App_1  (Dummy 0 = 198.51.100.10/32)   ← server endpoint, L3
           └─ SRV_Shop-App_2  (Dummy 0 = 198.51.100.11/32)
```

Both `Web` EPGs share **one** `Vlan110` segment (= BD `Web-BD`); the contract
`Web → App` between them appears in `gen_flow_list.csv`, not as a link.

---

## ⚠️ ACI concepts that can NOT be fully represented

> [!IMPORTANT]
> Network Sketcher is a VLAN/port diagram tool; ACI is a VXLAN policy fabric.
> The overlay is therefore a **faithful approximation, not a 1:1 model**. Keep
> these gaps in mind when reading it:
>
> - **Bridge Domain ≠ VLAN.** A real BD is a VXLAN L2 domain that can hold
>   **multiple EPGs each with its own, leaf-local encap VLAN**, and the fabric
>   forwards by VXLAN VNID — not by a single VLAN. The overlay collapses each BD
>   to **one representative `Vlan <id>`**. This is exact for a network-centric
>   design (1 BD = 1 EPG = 1 VLAN) but a simplification for application-centric
>   designs (many EPGs per BD). It also **assumes default flooding** — if
>   *Flood in Encapsulation* is enabled, per-EPG encaps are separate flood
>   scopes, which the diagram does not show.
> - **EPG is a policy group, drawn as a device.** NS has no "group" object, so
>   each EPG is a `PC`-stencil node. It is a *membership/segmentation* construct,
>   not a host.
> - **The VRF gateway is one node, but the real anycast gateway is distributed**
>   across every leaf where the BD is deployed. We draw a single
>   `<tenant>-<vrf>-GW` for readability.
> - **Contracts lose scope nuance.** Flows are the `provider × consumer`
>   cross-product within a tenant; `vzBrCP.scope` (global / VRF / tenant /
>   app-profile) and `vzCPIf` (exported contracts) are not enforced, so flows can
>   be over-stated.
> - **VMM-dynamic endpoints may be missing.** Endpoints (`fvCEp`) and their
>   leaf/port come from the operational fetch; an EPG deployed only via a VMM
>   domain with no static binding contributes no endpoints and gets a synthetic
>   BD VLAN id.
> - **Overlay links/ports are synthetic** (`Dummy N`, `Unknown` speeds) — they
>   encode BD/EPG membership and contract relationships, not cabling.
>
> Every overlay run also writes these as `INFERRED` / `MODEL` lines in
> `aci_overlay_report.md`. **Always validate against the APIC before relying on
> the diagram.**

---

## ACI → Network Sketcher mapping (summary tables)

### Underlay
| ACI | NS |
|-----|-----|
| `fabricNode` (or `fabricNodeIdentP`) role `spine` / `leaf` | `L3Switch` device (green) |
| role `controller` (APIC) | `Server` device (red; configurable via `treat_controller_as`) |
| Border leaf (referenced by an `l3extOut` node profile) | `L3Switch`, labelled "Border Leaf" |
| `topSystem` | per-node **version** (OS), **mgmt IP / TEP / pod** (Attribute-D) |
| `fabricExplicitGEp` / `fabricNodePEp` | **vPC pair** annotation on the paired leafs |
| `lldpAdjEp` adjacencies | **observed** spine/leaf/APIC L1 links (else inferred CLOS) |

### Overlay
See [Overlay representation rules](#overlay-representation-rules-read-this-before-reading-an-overlay-diagram)
above for the full detail; the one-line summary:

| ACI | NS |
|-----|-----|
| Tenant / VRF / BD / EPG / endpoint / L3Out / contract | area / `<tenant>-<vrf>-GW` + SVI / shared `Vlan <id>` segment / `<BD>-<AP>-<EPG>` device / `SRV_*`·`PC_*` host / cloud waypoint / `[Flow_List]` row |

## Device color conventions

The `rename attribute_bulk` command colours each device's **Default** cell. The
base palette is **shared across the sna / cv / cml / aci converters**:

| Colour | RGB | Meaning |
|--------|-----|---------|
| 🟩 Light green | `[235, 241, 222]` | **Observed network gear** — spine / leaf / border-leaf switch (underlay) |
| 🟥 Light red | `[255, 204, 204]` | **Server-role** — APIC controller (underlay) |
| 🟪 Light purple | `[221, 204, 255]` | **Logical / overlay** — every EPG, VRF gateway, L3Out, endpoint |
| ⬜ Light gray | `[200, 200, 200]` | **Inferred / external** — cloud waypoints |

**In aci_converter:** the **underlay** colours fabric nodes by role (switches
green, APIC red). The **overlay** colours *every* logical device light purple,
so it is instantly distinguishable from the physical fabric.

## Accuracy caveats

- **Underlay accuracy depends on the fetch.** With operational classes (the
  default) roles, the APIC controllers, hardware models, versions and the
  spine/leaf cabling are all **observed**. With `--no-topology` (config only) the
  APIC is absent, roles are inferred from name/nodeId, and the spine↔leaf mesh is
  **inferred** from ACI's guaranteed CLOS (synthetic but deterministic ports).
- **The overlay is a logical layout, not cabling** — see the emphasized section
  above; every synthetic element is listed under `INFERRED` / `MODEL` in
  `aci_overlay_report.md`. EPG/BD/subnet/VRF/contract/endpoint *data* is observed
  from the APIC.
- **Endpoint client/server is inferred** from contract direction; collapsing
  client segments follows the `sna_converter` convention.

## `fetch_from_apic.py` — pulling the model over the REST API

The converter reads local files, but `fetch_from_apic.py` grabs the model
straight from a reachable APIC and writes a `convert`-ready JSON. Retrieval is
**read-only** — it never modifies the fabric. Credentials come from a CLI arg or
the `ACI_PASSWORD` env var (never hard-coded); APIC self-signed certs are
accepted by default (`--verify-tls` to enforce).

**By default** it pulls both the policy model (`fabricNodeIdentP` + every
`fvTenant` subtree) **and** the operational + access-policy classes that make the
diagrams reflect the real fabric:

| Fetched (default) | Used for |
|-------------------|----------|
| `fabricNode`, `topSystem` | authoritative roles, **APIC controllers**, hardware model, version, mgmt/TEP/pod |
| `lldpAdjEp` | observed spine/leaf/APIC cabling |
| `fvCEp`, `fvIp`, `fvRsCEpToPathEp` | real endpoints (MAC/IP) and their leaf/port |
| `fabricExplicitGEp`, `fabricNodePEp`, `vpcDom` | vPC leaf pairs |
| `infraAttEntityP`, `fvnsVlanInstP`, `physDomP`, `infraHPortS`, `infraRsAccBaseGrp` | access policies (AEP / VLAN pools / domains / ports) |

Each class is fetched independently and a failed query is **skipped, not fatal**
— a partial fetch still produces the best diagram the data allows. Pass
**`--no-topology`** to fetch only the config-only policy model (smaller; the
underlay then falls back to inferred roles + CLOS and the overlay shows no
endpoints). A GUI Configuration Export is equivalent to a `--no-topology` fetch.

## Running the output in Network Sketcher

Each `*.txt` is a plain-text script (one command per line; `#` lines are phase
comments) already ordered Phase 1→6. Install
[Network Sketcher](https://github.com/cisco-open/network-sketcher), create an
empty master, run the command lines against it, and export the diagram. Paste
`gen_flow_list.csv` into the master's `[Flow_List]` sheet for the contract
traffic matrix.

Verified against the live `network-sketcher` MCP engine (and a real APIC): both
modes import with **0 failures** and every L1/L2/L3 diagram artifact generates.

## Configuration (`aci_to_ns_config.json`)

Every key is `{value, description, sample}`; only `value` is read. Key options:
`tenant_include` / `tenant_exclude` (overlay scope), `treat_controller_as`
(`Server` | `PC`), `spine_leaf_naming`, `border_leaf_node_ids`,
`apic_uplinks_per_leaf`, `vlan_base` (first synthetic BD VLAN id),
`max_endpoints_per_epg`, and `emit_bidirectional_flows`.

## Author

Yusuke Ogawa - Architect, Cisco | CCIE#17583

## License

This tool is part of the **Network Sketcher Cisco Extension** project, licensed
under the [Apache License 2.0](../LICENSE). See the [NOTICE](../NOTICE) file for
copyright and third-party attributions.
