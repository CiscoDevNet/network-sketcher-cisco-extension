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
| **Output** | `gen_master_commands.txt` (Network Sketcher CLI), `gen_flow_list.csv` (`[FLOW]` paste sheet), `out_of_scope_ips.csv` + `network_device_evidence.csv` (audit) |
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
| `--netdev-confidence` | from config | `off` / `certain` / `strong` / `medium` — lowest confidence tier promoted to an observed network device. Overrides `netdev_min_confidence` |

## Output files

For each input CSV, a folder `Output_data/<csv_name>/` is created containing:

| File | Description |
|------|-------------|
| `gen_master_commands.txt` | Network Sketcher CLI commands (areas, devices, L1 links, VLANs/SVIs, IPs, attributes) |
| `gen_flow_list.csv` | `[FLOW]` paste sheet: `Source/Destination Device Name` (master names), `TCP/UDP/ICMP`, `Service name(Port)`, `Max. bandwidth(Mbps)` |
| `out_of_scope_ips.csv` | Candidate server IPs that were **not** adopted, with the reason |
| `network_device_evidence.csv` | Every host that emitted network-only traffic, with the evidence, the confidence tier and what was done with it (see [Network device detection](#network-device-detection)) |
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

## Site grouping

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
| Existence of real network gear (`NWD_*`) | **Flow data** | A host that emits traffic only network gear produces (routing protocols, NetFlow export, HSRP/GLBP, BFD, CAPWAP, established BGP/LDP) is observed, not guessed. Its *role*, stencil and place in the topology remain inferred. See [Network device detection](#network-device-detection). |
| Subnets / VLAN segments (`/24`) | **Inferred** | Only the individual host IPs are observed. The `/24` prefix, the assumption that those IPs share one broadcast domain, and their modeling as VLANs/SVIs are all guesses — NetFlow carries no prefix length or VLAN information. |
| Sites / areas and their grouping | **Inferred** | Reconstructed from the inter-region (`/16`) traffic graph; tunable / overridable via `sna_to_ns_config.json`. |
| Datacenter vs client-site classification | **Inferred** | Heuristic from the server/client subnet mix. |
| Firewalls (FW), core switches, access switches, edge routers | **Inferred** | Synthesized infrastructure devices — they are **not** present in the flow data. Distinct from the observed `NWD_*` devices above. |
| WAN / Internet connectivity and links | **Inferred** | A plausible WAN/Internet edge is assumed; no link inventory exists in NetFlow. |
| L1 links between devices | **Inferred** | Reconstructed to connect the synthesized devices. |
| Device names | **Inferred** | Generated (servers are named from their adopted service ports; infra devices from site code + role). |
| Physical interface / port numbers on devices | **Inferred** | Assigned by the script; they do **not** correspond to real hardware ports. |
| SVIs / IP addressing of infrastructure devices | **Inferred** | Modeled from the inferred segments, not observed device configs. |

> In short: **IP addresses, L4 ports, protocols and the traffic matrix are
> ground truth from the flow data**, and so is the existence of the `NWD_*`
> devices. Everything else structural — the synthesised firewalls, cores and
> access switches, the WAN, links and physical port numbers — is a best-effort
> reconstruction that you should review and adjust.

## Network device detection

NetFlow carries no device inventory, but some traffic is emitted **only** by network
gear. Any inside host seen producing it is therefore *observed* infrastructure rather
than a guess, and is emitted as a green `NWD_*` device with a network stencil instead of
being mistaken for a red application server.

### Confidence tiers

| Tier | Evidence | Why it is conclusive |
|---|---|---|
| **certain** | `routing_protocol` (OSPF / EIGRP / PIM / VRRP / IS-IS as the IP protocol), `netflow_export` (UDP to a collector port), `hsrp_glbp` (UDP 1985/3222 to `224.0.0.2` / `224.0.0.102`), `bfd` (UDP 3784/3785/4784), `bgp_ldp` (TCP 179/646 with an observed SYN-ACK), `capwap_ap` / `capwap_wlc` (UDP 5246/5247) | Only routers, switches, WLCs and APs speak these. Promoted by default |
| **strong** | `aaa_client` (RADIUS 1645/1646/1812/1813), `tacacs_client` (TCP 49) | A RADIUS/TACACS+ client is normally a network access device, but a Windows NPS proxy also matches |
| **medium** | `snmp_agent` (polled on UDP 161), `syslog_trap_src` (UDP 514/162), `cisco_mac` (Cisco OUI) | Common on network gear, but servers are polled, log and can be Cisco-branded too |

Matching is deliberately strict: the **protocol must match and the port must be on the
server side of the flow**. Matching a bare port number against the ephemeral source port
produces large numbers of false positives — on a 400,000-flow production capture the
loose form reported 33 phantom HSRP speakers, 30 BFD and 5 CAPWAP devices, all of which
disappear under the strict rule.

### What you can and cannot expect

Detection is **asymmetric: a positive is conclusive, a negative proves nothing.** NetFlow
accounts for transit traffic, and Cisco's own documentation states that locally generated
traffic is not counted; NX-OS additionally does not record outgoing control-plane packets.
Add that NetFlow is usually enabled on only part of the estate, and it follows that plenty
of real network gear will emit no evidence at all. Use `netdev_force_ips` for devices you
know exist.

For the same reason **CDP and LLDP can never contribute**: they are non-IP Layer 2
protocols (EtherType `0x2000` / `0x88CC`), so NetFlow and IPFIX never see them. The same
applies to STP, VTP and DTP.

In practice, flow export is by far the most productive signal and routing protocols the
least. On the production capture mentioned above, 400,000 flows contained 145 EIGRP
records and exactly one OSPF record, but yielded 21 flow exporters.

### Role and stencil

The role is inferred from the evidence and drives the stencil; it is *not* observed:

| Role | Chosen when | Stencil |
|---|---|---|
| `Rtr` | routing protocol, HSRP/GLBP, BGP/LDP or BFD evidence | `Router` |
| `L3sw` | flow export (a routed data-path feature), or the host owns the `.1`/`.254` address | `L3Switch` |
| `Sw` | anything else | `Switch` |
| `WLC` / `AP` | CAPWAP, controller side / access point side | `WLC` / `AP` |

`Model` is left blank unless the export carried a real MAC vendor, in which case the vendor
string is used — inventing a model number for a device only seen in flow data would be
worse than admitting it is unknown.

### Effect on the rest of the pipeline

A confirmed network device is observed fact, so it bypasses the server heuristics: it needs
no service port to qualify, owning the `.1` gateway address no longer disqualifies it, and
a `/24` that is too quiet to be adopted on its own is force-adopted when it contains one.
Where a device owns the `.1` address, the synthetic site SVI moves to `.254` so the observed
address wins. Network devices take no part in the server-vs-client segment vote, so a router
living in a user VLAN does not turn it into a server segment.

Every candidate — promoted or not — is written to `network_device_evidence.csv` with its
evidence, tier and disposition, so the classification can be audited and tuned via
`netdev_min_confidence`, `netdev_force_ips` and `netdev_exclude_ips`. Setting
`detect_network_devices` to `false` (or `--netdev-confidence off`) restores the pre-0.6
behaviour byte-for-byte.

## Device naming conventions

The script generates device names according to the following rules.
Each name encodes the device type, location, and key metrics observed in the flow data.

| Device type | Name format | Example | Description |
|---|---|---|---|
| **Internet service** | `Svc_{proto}{port}_{n}` | `Svc_TCP443_4253` | One device per (protocol, port) combination observed as an external server. `{proto}` is `TCP` or `UDP`, `{port}` is the service port number, `{n}` is the number of **distinct external server IP addresses** observed for that service. All internet service devices share a single L2 segment on the `Internet` waypoint (`VlanIntSvc`). |
| **Intranet server** | `SRV_{site}_{ports}_{seq}` | `SRV_Camp_443-8080_3` | One device per inside server IP. `{site}` is the abbreviated site code, `{ports}` is a `-`-separated list of adopted service port numbers (those that exceed the byte/flow thresholds), `{seq}` is a per-site sequence number disambiguating servers with identical port sets. |
| **Client PC segment** | `PC_{site}_{n}_{seq}` | `PC_Camp1_36_2` | One device per client /24 segment (not classified as a server segment). `{site}` is the abbreviated site code, `{n}` is the number of **distinct client IP addresses** observed in that /24, `{seq}` is a per-site sequence number. |
| **Observed network device** | `NWD_{site}_{role}_{seq}` | `NWD_Data_Rtr_2` | One device per inside IP confirmed as real network gear. `{site}` is the abbreviated site code, `{role}` is `Rtr` / `L3sw` / `Sw` / `WLC` / `AP`, `{seq}` is a per-site, per-role sequence number. See [Network device detection](#network-device-detection). |

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

The generated `rename attribute_bulk` command writes a coloured cell into the **Default** column of the Network Sketcher Attribute sheet — `\"['DEVICE',[R,G,B]]\"` (WayPoints keep their token: `\"['WayPoint',[R,G,B]]\"`) — so every device is colour-coded by role in the Device Table. The palette and its meaning are **shared across every converter in this repo**:

| Colour | RGB | Meaning |
|--------|-----|---------|
| 🟩 Light green | `[235, 241, 222]` | **Observed network gear** — a real router / L3 switch / switch / firewall / WLC / AP present in the source data |
| 🟥 Light red | `[255, 204, 204]` | **Server-role endpoint** — server / controller / OT asset / internet service |
| 🟨 Light yellow | `[255, 255, 204]` | **Client endpoint** — PC / workstation / phone |
| 🟦 Light blue | `[220, 230, 242]` | **Observed network-device WayPoint** — a WayPoint backed by a real, observed network device (reserved; not emitted today, planned for future use) |
| ⬜ Light gray | `[200, 200, 200]` | **Inferred / not observed** — devices synthesised by the converter to complete a plausible topology, plus inferred WAN / Internet / cloud **WayPoints** (no real device behind them) |

The two WayPoint colours separate **observed** WayPoints (blue, backed by a real network device — future) from **inferred** WayPoints (gray, abstract WAN / Internet / cloud edges).

**In sna_converter:** the synthesised infrastructure stack (Core / FW / Edge router / Access switch / Server switch) is invented to complete the topology, so it is **gray**; network gear confirmed from network-only traffic (`NWD_*`) is **green**; observed servers (`SRV_*`) and internet services (`Svc_*`) are **red**; client PC segments (`PC_*`) are **yellow**; the `WAN` / `Internet` WayPoints have no real device behind them, so **gray**. The green/gray split therefore tells you at a glance which boxes in the diagram were actually seen in the flow data. Blue remains unused: a WayPoint would only turn blue if a specific observed device could be shown to *be* the WAN or Internet edge, and NetFlow carries nothing that identifies which device that is.

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
        ├── network_device_evidence.csv
        └── _normalized_flow.csv   (UI-format inputs only)
```

## Cisco Technologies

This tool bridges two Cisco technologies:

- **Cisco Secure Network Analytics (SNA / Stealthwatch)** — NetFlow-based
  network visibility and security analytics platform whose Flow Search export
  is the input.
- **Network Sketcher** — open-source Cisco tool for designing and documenting
  network topologies using an AI-native CLI.

## Author

Yusuke Ogawa - Architect, Cisco | CCIE#17583

## License

This tool is part of the **Network Sketcher Cisco Extension** project, licensed
under the [Apache License 2.0](../LICENSE). See the [NOTICE](../NOTICE) file for
copyright and third-party attributions.
