# netbox_converter — NetBox to Network Sketcher Command Converter

Convert a [NetBox](https://netboxlabs.com/oss/netbox/) DCIM/IPAM data set into a
ready-to-run [Network Sketcher](https://github.com/cisco-open/network-sketcher)
command script (L1/L2/L3). The model is pulled from NetBox over its **read-only
REST API** into a JSON file, and the converter turns that local file into a
Network Sketcher command script — so the conversion is reproducible **offline**.

```
NetBox REST API (read-only GET)  -->  netbox_export.json
                                            |
                                            v
                                   convert.py (NSModel)
                                            |
                                            v
                            ns_commands_netbox.txt  -->  Network Sketcher
```

> **Real devices are role-coloured** by the shared palette (green network gear /
> red server / yellow client), identical to every converter in this repo. An
> **observed WayPoint** — a WAN / provider WayPoint backed by a
> real NetBox record (`circuits.providernetwork`, e.g. "Level3 MPLS") — gets NS's
> native **light blue** `[220,230,242]`. Only **synthesised** shapes are forced
> light **gray** `[200,200,200]` to flag them as inferred rather than observed:
> the `dummy_stub_N` peers that stand in for uncabled VLAN/IP ports. See
> [Device color conventions](#device-color-conventions).

---

## Overview

| Item | Detail |
|------|--------|
| **Input** | NetBox core DCIM/IPAM via REST API (read-only GET) → `netbox_export.json` |
| **Output** | `ns_commands_netbox.txt` for Network Sketcher `run_commands`, plus debug/audit artefacts |
| **Dependencies** | Python 3.10+ and **`networkx`** (see below); everything else is stdlib |
| **NetBox connectivity** | Only for the fetch step (read-only GET); the conversion itself is local file I/O |
| **Validated against** | NetBox 4.6.3 public demo (`demo.netbox.dev`) — imports cleanly into Network Sketcher |

`networkx` is required for the graph-based topology layout — centrality
metrics and connected-component grouping determine each device's placement
tier.

---

## Why this converter — advantages over the Offline edition's NetBox import

The Network Sketcher **Offline edition** already ships a "NetBox" import, but
it is a **manual, L1-only CSV** path: the user hand-exports a spreadsheet with
just three columns — `Device`, `Interface`, `Connection` — and the tool
free-text-parses the `Connection` column to recover physical links. There is
**no NetBox API client anywhere in the offline edition**.

`netbox_converter` is a superset of that path. It uses the same proven
placement approach (graph centrality + connected-component grouping, so
diagrams lay out consistently with the offline tool), then adds everything the
offline CSV path cannot do:

| Capability | Offline edition (NetBox CSV) | **netbox_converter** |
|------------|------------------------------|----------------------|
| **Data ingestion** | Manual CSV export, produced out-of-band by the user | **Live read-only REST API fetch** (`fetch_from_netbox.py`) — token auth, pagination, `--site` filter, TLS; then fully offline conversion |
| **Layer coverage** | **L1 only** (physical links) | **L1 + L2 + L3** — cables, access/trunk **VLANs**, **SVIs**, interface **IP addresses**, per-interface **VRF**, **port-channels** (LAG) |
| **Patch panels / pass-through** | Whatever the CSV happens to contain | Resolved automatically via NetBox `connected_endpoints` (rear/front pass-through chains collapse to the real far end) |
| **WAN / circuit detection** | A bare device name in a CSV cell | **Structural** — `connected_endpoints_type` starts with `circuits.` → drawn as an *observed* WayPoint (light blue), robust to any provider name |
| **Icons / stencils** | Guessed from the device-name string only | Mapped from NetBox **role / platform / manufacturer** metadata |
| **Colour semantics** | None (no NetBox-derived colour) | Role palette (green/red/yellow) + observed WayPoint (light blue) + synthesised `dummy_stub` (gray) |
| **Uncabled VLAN/IP data** | Dropped (not in the L1 CSV) | Recovered via optional `dummy_stub_N` synthesis so the port — and its VLAN/IP — still appears |
| **NS engine validation** | L1-only, so it never meets the L2/L3 import constraints | Emits NS CLI commands with constraint guards (one-L1-link-per-port, interface-token normalisation, port-existence gating, PC-member L3 skip) for reliable import |
| **Runtime** | tkinter **desktop GUI** needing openpyxl / python-pptx / numpy | **Headless CLI** (stdlib + `networkx`); scriptable / CI-friendly |
| **Auditability** | — | Emits inventory CSV + `report.md` + intermediate NSModel JSON |

In short: the offline edition can only redraw the **physical wiring** you manage
to export by hand; `netbox_converter` pulls the **whole DCIM/IPAM model over the
API** and reconstructs L1 **and** L2/L3 — while keeping the offline tool's proven
layout.

---

## How devices are placed and connected

- **Connection (結線)** — devices are linked from each interface's
  `connected_endpoints` (NetBox resolves patch-panel / rear-front pass-through
  chains to the real far end, so patch panels drop out). Interfaces terminating
  on a circuit / provider network become a link to a shared gray **WAN cloud
  waypoint** (one per far-end name).
- **Placement (配置)** — devices are grouped by **connected component**
  ("network group"), NOT by NetBox site. Within each group every device gets a
  **tier** (0 = WAN/top … 7 = endpoint/bottom) from graph centrality + device
  role-name keywords, with redundant pairs pulled onto the same tier. The tier
  becomes the Network Sketcher grid row; column order minimises link crossings.
- Faithful to the offline tool, **only cabled devices are placed** by default
  (uncabled devices are omitted — set `include_unconnected: true` to keep them).

### Constraints enforced for Network Sketcher compatibility

Network Sketcher's command interface is stricter than the offline tool's
direct PPTX export, so the converter automatically enforces:

- **one L1 link per port** — NetBox multi-endpoint / fan-out is de-duplicated;
- **valid interface tokens** — non-Cisco names are normalised to NS-accepted
  tokens (`xe-0/0/0` → `TenGigabitEthernet 0/0/0`, `ge-` → `GigabitEthernet`,
  `et-` → `FortyGigabitEthernet`, foreign → `Ethernet <n>`);
- **ports must exist** — L2 / IP / SVI are emitted only for ports that NS will
  actually create (there is no standalone "add l1_interface" command), so VLANs
  / IPs on uncabled ports are skipped and reported.

---

## Quick Start

```bash
# 1. Install dependencies (networkx)
pip install -r netbox_converter/requirements.txt

# 2. Fetch the model from NetBox over the read-only REST API
#    (token via the NETBOX_TOKEN env var, or --token)
NETBOX_TOKEN=... python -m netbox_converter.src.fetch_from_netbox \
    --url https://demo.netbox.dev \
    --out netbox_converter/Input_data/netbox_export.json

# 3. Convert offline into a Network Sketcher command script
python -m netbox_converter.src.convert \
    -i netbox_converter/Input_data/netbox_export.json \
    -o netbox_converter/Output_data/ns_commands.txt \
    -c netbox_converter/netbox_to_ns_config.json
```

> **Run location:** `netbox_converter` lives under `3rd_party/`, which cannot be
> a Python package (it starts with a digit). Run the module commands from the
> `3rd_party/` directory (as shown, with paths prefixed by `netbox_converter/`),
> or from the repo root with `PYTHONPATH=3rd_party`.

Then in Network Sketcher: create an empty master and feed the non-comment lines
of `ns_commands_netbox.txt` to `run_commands`, then `build_default_outputs`.

---

## `fetch_from_netbox.py` — pulling the model over the REST API

Authenticates with a NetBox **API token** (`Authorization: Token <key>` — the
native scheme; `--bearer` switches to `Bearer`), then pulls only the **core
DCIM/IPAM** collections (no plugin / custom-field routes, for portability),
following NetBox `next` pagination to completion. Read-only (GET only).

| Flag | Meaning |
|------|---------|
| `--url` | NetBox base URL (e.g. `https://demo.netbox.dev`) — required |
| `--token` | API token (or `NETBOX_TOKEN` env var) |
| `--bearer` | Use `Authorization: Bearer` instead of `Token` |
| `--site` | Limit to a site slug (repeatable) |
| `--page-size` | REST pagination page size (default 500) |
| `--no-verify-tls` | Disable TLS verification (lab instances with self-signed certs) |
| `--out` | Output JSON path |

Collections fetched: `sites, locations, device-roles, platforms, manufacturers,
devices, interfaces, cables, ip-addresses, vlans, vrfs, prefixes`.

---

## `convert.py` — offline conversion

| Flag | Meaning |
|------|---------|
| `--input` / `-i` | NetBox export JSON (from `fetch_from_netbox.py`) — required |
| `--out` / `-o` | Base output path; side-outputs go to its directory |
| `--config` / `-c` | Path to `netbox_to_ns_config.json` (optional) |

Outputs (in the `--out` directory):

| File | Contents |
|------|----------|
| `<stem>_netbox.txt` | the NS command script |
| `ns_model_netbox.json` | the intermediate NSModel (debug) |
| `netbox_inventory.csv` | device → stencil mapping (audit) |
| `netbox_report.md` | counts + accuracy caveats |

### Config (`netbox_to_ns_config.json`)

| Key | Default | Meaning |
|-----|---------|---------|
| `draw_wan_waypoints` | `true` | Draw circuit / provider-network interfaces as a link to a gray WAN cloud (one per far-end name). |
| `synthesize_host_stubs` | `true` | Give an uncabled port that carries a VLAN / IP a synthetic `dummy_stub_N` peer (interface `Dummy <n-1>`) so the port — and its VLAN/IP — exists in NS. |
| `include_unconnected` | `false` | Keep uncabled devices in a dedicated `unlinked` area. Default false is offline-faithful (only connected devices are placed). |
| `color_overrides` | `{}` | `{ "device-name": [R,G,B] }` to override the role-based Default-cell colour for specific devices. |

---

## Data coverage & known limitations

| Data | Reflected? | Notes |
|------|-----------|-------|
| L1 links (cables / pass-through) | ✅ | via `connected_endpoints`; port de-duplicated |
| Port-channels (LAG) | ✅ | from interface `lag` membership |
| SVIs / loopbacks | ✅ | `virtual` interfaces (`VlanN` / `LoopbackN`) |
| Interface IPs + VRF | ✅ | on cabled ports / SVIs of placed devices |
| WAN / circuits | ✅ (opt) | drawn as a light-blue *observed* cloud WayPoint (`draw_wan_waypoints`) |
| Access/trunk VLANs on **uncabled** ports | ✅ (opt) | a `dummy_stub_N` peer is synthesized so the port exists (`synthesize_host_stubs`); disable to skip + report instead |
| IPs on **uncabled/absent** ports | ✅ (opt) | same `dummy_stub_N` mechanism |
| Unassigned IPAM addresses, VM (`virtualization.*`) interfaces | ➖ out of scope | not attached to a DCIM interface |
| Duplicate device names across sites | ⚠️ | collapse (model keyed by name) |

> ### ⚠️ Note — why `dummy_stub_N` exists, and when it appears
>
> **Why it is needed.** In Network Sketcher a port only comes into existence as
> the side-effect of an **L1 link** (or a port-channel) — there is no standalone
> "create this interface" command. So a port that carries a VLAN or an IP in
> NetBox but is **not cabled** has nowhere to live: NS would reject the L2/L3
> command with "interface not found". To keep that data instead of dropping it,
> the converter attaches such a port to a **synthetic peer** so the L1 link (and
> therefore the port, and therefore its VLAN/IP) exists.
>
> **When it appears.** Only when a device has one or more interfaces that (a) are
> **uncabled** in NetBox (no `connected_endpoints`) yet (b) carry a VLAN
> (`untagged_vlan` / `tagged_vlans`) or an interface **IP address**. Typical
> cause: host-facing access ports, or management/loopback data captured in NetBox
> without a modelled cable. Fully-cabled fabrics produce **no** stubs.
>
> **What it looks like.** **One** `dummy_stub_N` peer is created per affected
> device (not per port); every orphaned port links to it on its own `Dummy 0`,
> `Dummy 1`, … interface. Stubs are placed one tier below their parent and are
> the only shapes forced light **gray** `[200,200,200]` — a deliberate signal
> that **they are inferred placeholders, not real NetBox devices**. Do not read a
> `dummy_stub_N` box as an actual neighbour.
>
> **How to turn it off.** Set `synthesize_host_stubs: false` in the config: the
> converter then **skips** those uncabled VLANs/IPs instead and lists them in
> `netbox_report.md` (`skipped_l2_no_port` / `skipped_ip_no_port`), so you can
> see exactly what was left out.

Placement minimises L1 link crossings within each network group via a
Sugiyama-style layered sweep (median ordering + adjacent transpose, keeping the
lowest-crossing order).

`custom_fields` and plugin data are intentionally ignored so the converter works
against any NetBox instance.

---

## Device color conventions

The generated `rename attribute_bulk` command writes a coloured cell into the
**Default** column of the Network Sketcher Attribute sheet —
`\"['DEVICE',[R,G,B]]\"` (WayPoints keep their token: `\"['WayPoint',[R,G,B]]\"`)
— so every device is colour-coded by role. The palette is **shared across
every converter in this repo**:

| Colour | RGB | Meaning |
|--------|-----|---------|
| 🟩 Light green | `[235, 241, 222]` | **Observed network gear** — real router / L3 switch / switch / firewall / WLC / AP present in NetBox |
| 🟥 Light red | `[255, 204, 204]` | **Server-role endpoint** — device whose role maps to the `Server` stencil |
| 🟨 Light yellow | `[255, 255, 204]` | **Client endpoint** — PC / workstation / phone |
| 🟦 Light blue | `[220, 230, 242]` | **Observed WayPoint** — a WAN / provider WayPoint backed by a real source record (a NetBox `circuits.providernetwork`, e.g. "Level3 MPLS"). This is NS's native WayPoint colour and is what netbox_converter emits for observed WAN clouds. Also the fixed colour of the **Stencil Type** attribute column |
| ⬜ Light gray | `[200, 200, 200]` | **Inferred / synthesised** — placeholders that do not exist in the source: the `dummy_stub_N` peers that stand in for uncabled VLAN/IP ports |

> **Convention note — "Observed WayPoint" added to Light blue.** The shared
> palette originally reserved light blue for a network-device WayPoint that was
> *not emitted today* and used light gray for *all* WAN / cloud WayPoints.
> netbox_converter refines this: a WayPoint that is **observed** (backed by a
> real NetBox record) is emitted in **light blue** — NS's own native WayPoint
> colour — and light gray is reserved for **inferred / synthesised** shapes
> only. Other converters that draw *inferred* WAN clouds still colour them gray.

Two further fixed cell colours appear in every device row (set by the shared
`ns_command_builder`, not role-based): the **Model** column is pink
`[255, 183, 219]` and the **OS** column is light blue `[200, 230, 255]`.

**In netbox_converter:** every device NetBox reports is *observed*, so real
network gear renders **green**, `Server`-role devices **red** and PC/phone
endpoints **yellow** — the same rule as the other physical-fabric converters.
An observed WAN / provider WayPoint (e.g. `Level3 MPLS`, drawn via
`draw_wan_waypoints`) renders **light blue**. The only **inferred** shape is the
`dummy_stub_N` peer synthesised to host an otherwise-uncabled VLAN/IP port, which
is forced **gray**. A device can be recoloured explicitly via the
`color_overrides` config key.

---

## Running the output in Network Sketcher

`ns_commands_netbox.txt` is a plain-text script (one command per line; `#` lines
are phase comments) already ordered Phase 1→6. Install
[Network Sketcher](https://github.com/cisco-open/network-sketcher), create an
empty master, run the non-comment command lines in order against it, then export
the L1/L2/L3 diagram + device table. See the
[top-level README "Running the output in Network Sketcher"](../../README.md#running-the-output-in-network-sketcher)
for step-by-step instructions.

The bundled sample export (NetBox 4.6 public demo, 34 placed devices) imports
cleanly and generates the full diagram set.

---

## Author

Yusuke Ogawa - Architect, Cisco | CCIE#17583

## License

This tool is part of the **Network Sketcher Cisco Extension** project, licensed
under the [Apache License 2.0](../../LICENSE). See the [NOTICE](../../NOTICE)
file for copyright and third-party attributions.
