# sna_to_nsm — Cisco SNA / NetFlow CSV to Network Sketcher Converter

Convert a [Cisco Secure Network Analytics (SNA / Stealthwatch)](https://www.cisco.com/site/us/en/products/security/security-analytics/secure-network-analytics/index.html)
NetFlow **Flow Search CSV export** into a ready-to-run
[Network Sketcher](https://github.com/cisco-open/network-sketcher) command
script **and** a `[FLOW]` traffic CSV — no SNA server connection required.

> Network Sketcher can draw a topology from device/link data, but it has no
> notion of *who actually talks to whom*. `sna_to_nsm` reconstructs a plausible
> **multi-site Layer 1/2/3 topology** (sites, VLANs/SVIs, firewalls, cores,
> access switches) **and the endpoints** (servers, client segments, Internet
> services) purely from observed NetFlow, then emits the per-flow traffic
> matrix you can paste into a Network Sketcher `[FLOW]` sheet.

---

## Overview

| Item | Detail |
|------|--------|
| **Input** | Cisco SNA Flow Search CSV — **API format** (`searchSubject.* / peer.*`) or **UI export format** (`Subject IP Address`, `Total Bytes`="56.49 M", `Duration`="36min 38s") — auto-detected |
| **Output** | `gen_master_commands.txt` (Network Sketcher CLI), `gen_flow_list.csv` (`[FLOW]` paste sheet), `out_of_scope_ips.csv` (audit) |
| **Dependencies** | Python 3.8+ standard library only — no pip packages |
| **Platforms** | Windows, macOS, Ubuntu/Linux |
| **SNA connectivity** | None — purely local file I/O |

## Quick Start

```bash
# 1. (Optional) install dependencies — there are none, this is a no-op
pip install -r requirements.txt

# 2. Drop one or more SNA Flow CSVs into Input_data/
#    (a ready-made sample_flows.csv is included)

# 3. Convert every CSV in Input_data/  (run from the project folder)
python sna_to_ns_commands.py

# Results appear under Output_data/<csv_name>/
```

### Other ways to run

```bash
# Convert a single CSV (anywhere on disk)
python sna_to_ns_commands.py path/to/flowAnalysis.csv

# Convert every CSV in a specific folder
python sna_to_ns_commands.py path/to/folder

# Use a custom settings file and choose which endpoints to emit
python sna_to_ns_commands.py --config my_config.json --endpoints both

# Custom input/output folders
python sna_to_ns_commands.py --input-dir captures --output-dir results
```

| Option | Default | Meaning |
|--------|---------|---------|
| `input` (positional) | — | A single CSV **file** or a **folder**. If omitted, all CSVs in `--input-dir` are processed |
| `--input-dir` | `Input_data` | Folder scanned for `*.csv` in batch mode |
| `--output-dir` | `Output_data` | Output root; each CSV writes to `<output-dir>/<csv_name>/` |
| `--endpoints` | `both` | `none` / `servers` / `clients` / `both` — which endpoint devices to generate |
| `--config` | `sna_to_ns_config.json` | Path to the settings JSON |
| `--server-min-flows` | `1` | Extra lower bound on flows for a service to be adopted |
| `--no-flow` | off | Skip generating `gen_flow_list.csv` |

## Output files

For each input CSV, a folder `Output_data/<csv_name>/` is created containing:

| File | Description |
|------|-------------|
| `gen_master_commands.txt` | Network Sketcher CLI commands (areas, devices, L1 links, VLANs/SVIs, IPs, attributes) |
| `gen_flow_list.csv` | `[FLOW]` paste sheet: `Source/Destination Device Name` (master names), `TCP/UDP/ICMP`, `Service name(Port)`, `Max. bandwidth(Mbps)` |
| `out_of_scope_ips.csv` | Candidate server IPs that were **not** adopted, with the reason |
| `_normalized_flow.csv` | Present only when the input was UI-format; the normalized intermediate the tool actually processed |

### How `Max. bandwidth(Mbps)` is computed

`Max. bandwidth = total bytes (sent + received) x 8 / session active time (s) / 1,000,000`

For each unique `(Source, Destination, protocol, port)` combination the
**maximum** Mbps across all matching flows is kept. Values below 1 Mbps are
shown as plain decimals (e.g. `0.0476`); zero-duration flows are skipped.

## Running the output in Network Sketcher

`gen_master_commands.txt` is a plain-text script (one command per line). Install
[Network Sketcher](https://github.com/cisco-open/network-sketcher), create an
empty master, and run the command lines in order against it (e.g. via the
`run_commands` interface), then export the diagram. The `gen_flow_list.csv`
content can be pasted into the master's `[Flow_List]` sheet (the `Manually /
Automatic routing path settings` columns are left blank by design).

To actually render these flows on top of the exported PPTX diagram, follow the
Network Sketcher wiki guide
[9‑4. Adding communication flows to the diagram file](https://github.com/cisco-open/network-sketcher/wiki/9%E2%80%904.adding-communication-flows-to-the-diagram-file).

## Supported SNA CSV formats

The input format is auto-detected from the header row:

- **API format** — machine-readable column names such as
  `searchSubject.ipAddress`, `peer.ipAddress`, `peer.portProtocol.port`,
  `connection.transferBytes`, `activeDuration`.
- **UI export format** — human-readable columns such as `Subject IP Address`,
  `Peer Port/Protocol` (`2055/UDP`), `Total Bytes` (`56.49 M`),
  `Duration` (`36min 38s`). These are normalized automatically (byte
  suffixes K/M/G/T, duration `d/h/min/s`, `port/proto` strings).

## How it works

```
SNA Flow CSV (API or UI format)
   └─► normalize_csv()        detect format, convert UI -> required columns
         └─► inside detection  RFC1918 + auto/declared public ranges
               └─► /24 aggregation (segments) -> /16 regions
                     └─► traffic-graph grouping -> sites (+ DC / spur split)
                           └─► server detection  (SNA orientation: peer = server)
                                 └─► command builder -> gen_master_commands.txt
                                 └─► flow matrix     -> gen_flow_list.csv
```

Site grouping is driven by the settings in `sna_to_ns_config.json`. Highlights:

- **Auto-detection** of inside public ranges, Datacenter regions and isolated
  "spur" sites, all overridable by hand (manual settings win).
- **`site_cidrs`** — define sites explicitly by arbitrary CIDR (any prefix
  length), overriding the automatic `/16` grouping.
- **`name_map`** — rename auto-grouped hub sites to friendly names.

Every parameter in `sna_to_ns_config.json` carries an inline `description` and
`sample` value documenting its meaning and an example setting.

## What comes from flow data vs what is inferred

NetFlow tells you **who talked to whom, on which L4 port, and how much** — but it
contains **no device or link inventory**. Everything in the diagram is therefore
either read directly from the flow records or *reconstructed* (inferred) by the
script. The table below makes the boundary explicit so you know what to trust
as-is and what to review before treating the topology as authoritative.

| Item | Source | Notes |
|------|--------|-------|
| Host / endpoint IP addresses | **Flow data** | Read verbatim from `searchSubject.ipAddress` / `peer.ipAddress`. |
| L4 service ports (e.g. 443, 445, 1433, 53) | **Flow data** | From `peer.portProtocol.port`; used as the service identity. |
| Protocol (TCP / UDP) | **Flow data** | From the flow record's protocol field. |
| Bytes transferred / session duration | **Flow data** | Used to compute `Max. bandwidth(Mbps)` in the `[FLOW]` matrix. |
| Server vs client role of each host | **Flow data** | Derived from SNA orientation (`peer = server`) and the TCP SYN-ACK / byte thresholds. |
| `[FLOW]` traffic matrix (source/dest/proto/port/Mbps) | **Flow data** | Aggregated directly from observed conversations. |
| Subnets / VLAN segments (`/24`) | **Inferred** | Only the individual host IPs are observed. The `/24` prefix, the assumption that those IPs share one broadcast domain, and their modeling as VLANs/SVIs are all guesses — NetFlow carries no prefix length or VLAN information. |
| Sites / areas and their grouping | **Inferred** | Reconstructed from the inter-region (`/16`) traffic graph; tunable / overridable via `sna_to_ns_config.json`. |
| Datacenter vs client-site classification | **Inferred** | Heuristic from the server/client subnet mix. |
| Firewalls (FW), core switches, access switches, edge routers | **Inferred** | Synthesized infrastructure devices — they are **not** present in the flow data. |
| WAN / Internet connectivity and links | **Inferred** | A plausible WAN/Internet edge is assumed; no link inventory exists in NetFlow. |
| L1 links between devices | **Inferred** | Reconstructed to connect the synthesized devices. |
| Device names | **Inferred** | Generated (servers are named from their adopted service ports; infra devices from site code + role). |
| Physical interface / port numbers on devices | **Inferred** | Assigned by the script; they do **not** correspond to real hardware ports. |
| SVIs / IP addressing of infrastructure devices | **Inferred** | Modeled from the inferred segments, not observed device configs. |

> In short: **IP addresses, L4 ports, protocols and the traffic matrix are
> ground truth from the flow data.** Everything structural — firewalls,
> switches, routers, the WAN, links, device names and physical port numbers — is
> a best-effort reconstruction that you should review and adjust.

## Device naming conventions

The script generates device names according to the following rules.
Each name encodes the device type, location, and key metrics observed in the flow data.

| Device type | Name format | Example | Description |
|---|---|---|---|
| **Internet service** | `Svc_{proto}{port}_{n}` | `Svc_TCP443_4253` | One device per (protocol, port) combination observed as an external server. `{proto}` is `TCP` or `UDP`, `{port}` is the service port number, `{n}` is the number of **distinct external server IP addresses** observed for that service. All internet service devices share a single L2 segment on the `Internet` waypoint (`VlanIntSvc`). |
| **Intranet server** | `SRV_{site}_{ports}_{seq}` | `SRV_Camp_443-8080_3` | One device per inside server IP. `{site}` is the abbreviated site code, `{ports}` is a `-`-separated list of adopted service port numbers (those that exceed the byte/flow thresholds), `{seq}` is a per-site sequence number disambiguating servers with identical port sets. |
| **Client PC segment** | `PC_{site}_{n}_{seq}` | `PC_Camp1_36_2` | One device per client /24 segment (not classified as a server segment). `{site}` is the abbreviated site code, `{n}` is the number of **distinct client IP addresses** observed in that /24, `{seq}` is a per-site sequence number. |

### Site code abbreviations

Site code (`{site}`) is a short prefix derived from the auto-detected site name:

| Site type | Example site name | Example code |
|---|---|---|
| First campus hub | `Campus-10.201` | `Camp` |
| Second campus hub | `Campus-10.10` | `Camp1` |
| Datacenter | `Datacenter` | `Data` |
| Isolated site (spur) | `Site-10-30` | `Site` (+ numeric suffix for 2nd, 3rd …) |

Use `name_map` in `sna_to_ns_config.json` to rename hub sites (and thus their code prefixes) to friendly labels.

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

**In sna_converter:** every network device (Core / FW / Edge router / Access switch / Server switch) is synthesised from the flow data, so it is **gray**; observed servers (`SRV_*`) and internet services (`Svc_*`) are **red**; client PC segments (`PC_*`) are **yellow**; the `WAN` / `Internet` WayPoints are inferred, so **gray**. Observed network gear renders **green**, kept consistent with the other converters for any device the flow data surfaces as real.

## Directory structure

```
sna_converter/
├── README.md                  (this file)
├── requirements.txt
├── .gitignore
├── sna_to_ns_commands.py      ← entry point
├── sna_to_ns_config.json      ← settings (value / description / sample per key)
├── Input_data/                ← place your SNA CSVs here
│   └── sample_flows.csv        (included example)
└── Output_data/               ← results, one subfolder per input CSV
    └── <csv_name>/
        ├── gen_master_commands.txt
        ├── gen_flow_list.csv
        ├── out_of_scope_ips.csv
        └── _normalized_flow.csv   (UI-format inputs only)
```

## Cisco Technologies

This tool bridges two Cisco technologies:

- **Cisco Secure Network Analytics (SNA / Stealthwatch)** — NetFlow-based
  network visibility and security analytics platform whose Flow Search export
  is the input.
- **Network Sketcher** — open-source Cisco tool for designing and documenting
  network topologies using an AI-native CLI.

## License

Apache License 2.0.
