# meraki_converter — Cisco Meraki to Network Sketcher Command Converter

Convert a [Cisco Meraki](https://meraki.cisco.com/) organization into a
ready-to-run [Network Sketcher](https://github.com/cisco-open/network-sketcher)
command script (L1/L2/L3) — no Meraki server access needed at conversion time.

Data acquisition is **API-based**: a read-only client pulls the org over the
**Meraki Dashboard API v1** into a JSON file, and the converter turns that local
file into a Network Sketcher command script — so the conversion is reproducible
offline (e.g. after a DevNet Sandbox reservation expires).

```
Meraki Dashboard API v1  --(read-only GET)-->  meraki_export.json
                                                     |
                                                     v
                                           convert.py (NSModel)
                                                     |
                                                     v
                                      ns_commands_meraki.txt  -->  Network Sketcher
```

> The Network Sketcher Offline edition has no native Meraki import.
> `meraki_converter` reconstructs **Layer 1, 2 and 3** (links, VLANs, SVIs, IPs,
> VRFs, HA/AutoVPN/LACP) from the Dashboard API and renders them under an
> `Internet` cloud waypoint.

---

## Overview

| Item | Detail |
|------|--------|
| **Input** | Cisco Meraki org via Dashboard API v1 (read-only GET) → `meraki_export.json` |
| **Output** | `ns_commands_meraki.txt` ready for Network Sketcher `run_commands`, plus debug/audit artefacts |
| **Dependencies** | Python 3.8+, **standard library only** (no third-party packages) |
| **Meraki connectivity** | Only for the fetch step (read-only GET); the conversion itself is purely local file I/O |

## ⚠️ Verification status (read this first)

This converter has only been run end-to-end against the **DevNet *Reservable*
Meraki Sandbox**: a single **flat (VLAN-disabled)** network of four **virtual,
dormant** devices (MX100, MS250-24, MR52, MV12W). Because those devices carry no
traffic and are not truly online, every "live/dynamic" endpoint returned
**empty / disabled / null**. As a result several features are **implemented and
unit-tested with synthetic data, but have never been validated against real
Dashboard API responses** — marked **🧪 Unverified** below. All such paths
**fall back gracefully** to the verified behaviour when their data is absent, so
they do not affect the sandbox output, but their real-world field shapes are
unproven until run against a production org.

"Verified" = exercised with real sandbox API data → NS import → diagram.

| Capability | Status |
|------------|--------|
| Device inventory / stencil mapping | ✅ Verified |
| Synthesized L1 branch + Internet waypoint | ✅ Verified |
| Single-LAN MX LAN gateway (SVI + IP) | ✅ Verified |
| MX WAN IP + default-gateway IP on Internet side | ✅ Verified |
| Switch **access**-port VLAN → L2 | ✅ Verified |
| Tier layout (Internet top, siblings same row, crossing-min) | ✅ Verified |
| MX HA **role** note | ✅ Verified (role only) |
| NS import + L1/L2/L3 + device table | ✅ Verified |
| Discovered L1 from LLDP/CDP | 🧪 Unverified — not exercised in the validation sandbox |
| Live port selection (Connected / isUplink) | 🧪 Unverified — not exercised in the validation sandbox |
| Switch SVIs / VRF | 🧪 Unverified — not exercised in the validation sandbox |
| Trunk multi-VLAN (`allowedVlans`/`all` → universe) | 🧪 Unverified — not exercised in the validation sandbox |
| Appliance per-VLAN SVIs (VLANs enabled) | 🧪 Unverified — not exercised in the validation sandbox |
| MX HA pair (warm-spare primary/spare) | 🧪 Unverified — not exercised in the validation sandbox |
| Device management IPs (`lanIp`) | 🧪 Unverified — not exercised in the validation sandbox |
| AutoVPN spoke→hub tunnels + BGP AS | 🧪 Unverified — not exercised in the validation sandbox |
| LACP port-channels | 🧪 Unverified — not exercised in the validation sandbox |
| Static routes (appliance / switch) | 🧪 Fetched, not rendered |

## Quick Start

```bash
# 1. (no install needed — Python 3.8+ standard library only)

# 2. Get a read-only Meraki API key and the org id (run from the repository root)
export MERAKI_API_KEY=...          # Windows PowerShell: $env:MERAKI_API_KEY="..."

# 3. Fetch the org over the read-only Dashboard API -> JSON
python -m meraki_converter.src.fetch_from_meraki \
    --org-id <ORG_ID> \
    --out    meraki_converter/Input_data/meraki_export.json
#   options: --network-id <id> (repeatable), --include-clients, --api-key <key>

# 4. Convert the saved JSON (no live API needed)
python -m meraki_converter.src.convert \
    -i meraki_converter/Input_data/meraki_export.json \
    -o meraki_converter/Output_data/ns_commands_meraki.txt \
    -c meraki_converter/meraki_to_ns_config.json
```

`fetch_from_meraki` issues only GET requests — it never modifies the org.

### Getting a Meraki API key (read-only is sufficient)

In the Meraki Dashboard: **Organization → Settings → Dashboard API access**
(enable), then **My profile → API access → Generate new API key**. The key is
**per-user**, not per-org, and grants access to every org you administer, so
prefer a key scoped to a Sandbox/lab org and verify scope with
`GET /organizations` first. For DevNet Sandboxes use a **Meraki personal
account** (not Azure SAML) and create the key directly on the Sandbox org.

## Output files

| File | Description |
|------|-------------|
| `ns_commands_meraki.txt` | Network Sketcher CLI commands (Phase 1–6) — main deliverable |
| `ns_model_meraki.json` | Intermediate topology model for debugging |
| `meraki_inventory.csv` | Device → stencil-type mapping (audit) |
| `meraki_report.md` | Counts + per-network accuracy caveats (`DISCOVERED` vs `SYNTHESIZED`) |

## Device naming conventions

Real Meraki devices keep their **inventory identity** — `<Model>_<last-4-of-serial>`
(e.g. `MX100_YL5K`, `MS250-24_LVYP`, `MR52_DGPN`, `MV12W_LMGX`). This applies to
all network gear *and* server-role devices (the MV camera), so the model stays
visible in the diagram. **Synthesised endpoints** instead use the shared
`PC_/SRV_` scheme aligned with the [sna_converter](../sna_converter/README.md#device-naming-conventions):

| Device type | Name format | Example | Description |
|---|---|---|---|
| **Real Meraki device** | `<Model>_<serial4>` | `MR52_DGPN` | Network gear (MX/MS/MR) and server-role devices (MV camera) keep model + last-4-of-serial. Coloured by role (green / red). |
| **Dummy wireless client** | `PC_{site}_0_{seq}` | `PC_Bran_0_1` | One synthetic PC per **enabled SSID**, hung off the MR's wireless IF (not the switch). `{n}`=`0` (a placeholder — no real client count is known); `{seq}` is a per-network sequence. Rendered **gray** (inferred device). |
| **Observed client** | `PC_{site}_{n}_{seq}` | `PC_Bran_1_2` | Emitted only with `--include-clients`; one per observed client (`{n}`=`1`, one real IP each). Rendered **yellow** (observed endpoint). |

`{site}` is the **first 4 alphanumeric characters of the network name**,
capitalised (`branch_office` → `Bran`), matching the sna_converter site-code
style. Because the dummy PC is a stand-in for an SSID's wireless clients, it
connects **only** to the MR's wireless IF (`<SSID name> <n>`) and is never wired
to the upstream switch.

**Endpoints are modelled as Layer-3 interfaces.** PC and server devices (the
dummy wireless-client PCs, observed clients, and the MV camera) carry **no L2
segment** — their connecting port is a routed L3 interface (the IP lands there
directly when one is known, per RULE 11.5). Only network gear (MX/MS/MR) holds
L2 segments; the AP's wireless IF carries the SSID's L2 segment, but the dummy PC
on the other end of that link does not.

## Device color conventions

The generated `rename attribute_bulk` command writes a coloured cell into the
**Default** column of the Network Sketcher Attribute sheet —
`\"['DEVICE',[R,G,B]]\"` (WayPoints keep their token: `\"['WayPoint',[R,G,B]]\"`)
— so every device is colour-coded by role. The palette is **shared across
every converter in this repo**:

| Colour | RGB | Meaning |
|--------|-----|---------|
| 🟩 Light green | `[235, 241, 222]` | **Observed network gear** — real router / L3 switch / switch / firewall / WLC / AP present in the source data |
| 🟥 Light red | `[255, 204, 204]` | **Server-role endpoint** — server / controller / camera / internet service |
| 🟨 Light yellow | `[255, 255, 204]` | **Client endpoint** — PC / workstation / phone |
| 🟦 Light blue | `[220, 230, 242]` | **Observed network-device WayPoint** (reserved; not emitted today) |
| ⬜ Light gray | `[200, 200, 200]` | **Inferred / not observed** — synthesised devices + inferred WAN / Internet / cloud **WayPoints** |

Two further fixed cell colours appear in every device row (set by the shared `ns_command_builder`, not role-based): the **Model** column is pink `[255, 183, 219]` and the **OS** column is light blue `[200, 230, 255]`.

**In meraki_converter:** every device in the org is observed, so network gear
(MX `Firewall`, MS `L3Switch`/`Switch`, MR `AP`) renders **green** and the MV
camera (mapped to `Server`, as NS has no Camera stencil) renders **red**. The
synthesized `Internet` cloud is an inferred WayPoint, so **gray**, as is each
**dummy wireless-client PC** (`PC_{site}_0_{seq}`) hung off an MR — both are
inferred, not observed.

## Running the output in Network Sketcher

`ns_commands_meraki.txt` is a plain-text script (one command per line; `#` lines
are phase comments) already ordered Phase 1→6. Install
[Network Sketcher](https://github.com/cisco-open/network-sketcher), create an
empty master, run the command lines in order against it, and export the L1/L2/L3
diagram + device table. See the
[top-level README "Running the output in Network Sketcher"](../README.md#running-the-output-in-network-sketcher)
for step-by-step instructions.

## How it works

### L1: discovered or synthesized

Meraki exposes a discovered link map (`topology/linkLayer`) and per-device
neighbours (`devices/{serial}/lldpCdp`), but both depend on live CDP/LLDP. For
offline or **virtual** devices — including the DevNet Reservable Meraki Sandbox,
whose devices are "for API use only" — these return empty. The converter then
**synthesizes** a standard MX→MS→MR/MV branch topology from the inventory; those
links are **inferred, not observed**, and that is recorded per-network in
`meraki_report.md`. When real neighbours are present they take priority
(🧪 unverified against live data — see the matrix above).

### MR access points: L2 bridge + management SVI

A Meraki MR is a **Layer 2 bridge** with a single management IP on its native
(untagged) VLAN — it has no real SVI and does not route. The converter models it
that way (toggle `ap_management_model`):

- the wired uplink (`GigabitEthernet 0/0`) carries the native L2 segment, plus
  any VLAN-tagged SSID VLANs (so a tagging deployment renders as a trunk, an
  untagged one as a single-segment access port);
- a synthetic `Dummy_mgmt 0` management SVI is bound to the native segment and
  receives the AP's management IP (`lanIp`) **only when one exists** (null on the
  dormant sandbox devices, populated on a live org);
- each **enabled** SSID becomes a **physical port** L1-linked to the same switch
  the AP uplinks to, named `<SSID name> <n>` (e.g. `branch_office-wirelessWiFi 0`)
  or `wlan <n>` when the SSID name is unknown, numbered per SSID. It is bound to
  its L2 segment (native if untagged, `Vlan<id>` if VLAN-tagged).

The management IP therefore lands on the SVI (correct for an L2 device), not on
the physical port. Cameras (MV) and sensors (MT) keep the simpler model (`lanIp`
on the physical uplink). Set `ap_management_model` to `false` to disable.

> **Note — native L2 segment name follows the connected switch.** Even though an
> MR's native VLAN is *untagged*, the converter does **not** invent a separate
> name (e.g. `untag`) for it. A standalone name produces a separate Network
> Sketcher broadcast domain that does not merge with the switch's native VLAN, so
> the AP would appear isolated at L2. Instead the AP reuses the **exact L2 segment
> name of the switch port it connects to** (`Vlan1` in the sandbox), so the AP
> and its switch share one broadcast domain. `ap_untagged_segment_name` is only a
> fallback for an AP that has no upstream switch (e.g. attached straight to the
> MX). Also note NS stores an L1 port name as a single `type number` pair and
> strips inner spaces from the type token, so the converter pre-strips spaces in
> the SSID name (`branch_office - wireless WiFi` → `branch_office-wirelessWiFi`)
> to keep the L1-link and L2-segment names identical.

## Endpoints fetched

Org/inventory: `organizations/{org}`, `.../networks`, `.../devices`,
`.../devices/statuses`, `.../appliance/uplink/statuses`.
Per device: `managementInterface`, `lldpCdp`, and for switches `switch/ports`,
`switch/ports/statuses`, `switch/routing/interfaces`, `switch/routing/staticRoutes`.
Per network: `appliance/{vlans,singleLan,ports,staticRoutes,settings,warmSpare}`,
`appliance/vpn/{siteToSiteVpn,bgp}`, `switch/linkAggregations`, `wireless/ssids`,
`topology/linkLayer` (and `clients` with `--include-clients`).
Endpoints that error for a given network (e.g. `appliance/vlans` → *"VLANs are
not enabled"* on a flat network) are **skipped gracefully**, leaving the
corresponding feature out.

## Configuration (`meraki_to_ns_config.json`)

Each key has a `value` (used by the tool) plus `description`/`sample`.

| Key | Default | Purpose |
|-----|---------|---------|
| `network_include` | `[]` | If non-empty, only these network IDs/names are converted. |
| `network_exclude` | `[]` | Network IDs/names to skip. |
| `synthesize_l1` | `true` | Build the branch topology when no real links are present. |
| `add_internet_waypoint` | `true` | Add an `Internet` cloud and link each MX WAN port to it. |
| `include_clients` | `false` | Draw network clients as `PC` endpoints (requires `--include-clients` at fetch). |
| `stencil_overrides` | `{}` | Map NS device name → NS Stencil Type. |
| `color_overrides` | `{}` | Map NS device name → `[R,G,B]` cell colour. |
| `ap_management_model` | `true` | Model each MR AP as an L2 bridge: native uplink segment (matching the connected switch), `Dummy_mgmt 0` management SVI (carries `lanIp` when present), and a physical `<SSID> <n>`/`wlan <n>` port per enabled SSID L1-linked to the switch. `false` puts `lanIp` on the physical port. |
| `ap_untagged_segment_name` | `"untag"` | **Fallback** native L2 segment name for an MR AP that has *no* upstream switch (attached straight to the MX). When the AP connects to a switch it reuses the switch port's segment name instead, to share the broadcast domain. |

## Notes / limitations

- **Most dynamic features are unverified against live data** — see the
  [Verification status](#️-verification-status-read-this-first). They are
  unit-tested and fall back safely, but field names/shapes from a real org may
  differ and need a live run to confirm.
- **VRF** is only populated from Catalyst-on-Meraki `switch/routing/interfaces`
  (`vrfName`); classic MX/MS expose little/no VRF in the Dashboard API.
- Trunks with `allowedVlans: "all"` are expanded to the network's **real** VLAN
  set (appliance VLANs + switch access/native + SVIs), capped at 64.
- Appliance/switch **static routes** are fetched but **not drawn** (no NS
  representation yet).
- The MV camera maps to the NS `Server` stencil (NS has no Camera stencil).

## Directory structure

```
meraki_converter/
├── README.md                     (this file)
├── requirements.txt              ← no third-party deps (stdlib only; no-op install)
├── .gitignore                    ← ignores generated output + org-specific input
├── meraki_to_ns_config.json      ← settings (value / description / sample per key)
├── Input_data/                   ← saved meraki_export.json (+ raw/ API snapshots)
│   └── meraki_export.json
├── Output_data/                  ← results
│   ├── ns_commands_meraki.txt
│   ├── ns_model_meraki.json
│   ├── meraki_inventory.csv
│   └── meraki_report.md
└── src/                          (entry points: fetch_from_meraki.py, convert.py)
```

## Cisco Technologies

This tool bridges two Cisco technologies:

- **Cisco Meraki** — cloud-managed networking (MX security appliances, MS
  switches, MR access points, MV cameras) administered through the Meraki
  Dashboard and its read-only Dashboard API v1, the data source here.
- **Network Sketcher** — open-source Cisco tool for designing and documenting
  network topologies using an AI-native CLI.

## Author

Yusuke Ogawa - Architect, Cisco | CCIE#17583

## License

This tool is part of the **Network Sketcher Cisco Extension** project, licensed
under the [Apache License 2.0](../LICENSE). See the [NOTICE](../NOTICE) file for
copyright and third-party attributions.
