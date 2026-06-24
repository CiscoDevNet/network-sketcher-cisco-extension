# Network Sketcher Cisco Extension — bridge Cisco platforms to automatic network diagrams

**Network Sketcher Cisco Extension** is a growing collection of tools that turn
Cisco platform data into
[Network Sketcher](https://github.com/cisco-open/network-sketcher) CLI commands,
so you can rebuild an accurate L1/L2/L3 topology — devices, links, VLANs, SVIs,
sub-interfaces, IP addressing and VRFs — in seconds instead of drawing it by
hand.

> [!IMPORTANT]
> **The quality of each converted topology depends entirely on what each Cisco
> product is able to export.** Because the exports rarely contain a complete
> physical/logical inventory, the generated diagrams necessarily include a
> significant amount of **inference and synthesized structure**. Treat the output
> as a **starting reference / first draft only** — always review and validate it
> against authoritative sources before relying on it.

Each tool targets a different Cisco data source, can be used independently, and
the **conversion runs entirely on local files** (no live platform connection is
needed at conversion time). Most tools read a file you export from the platform;
`aci_converter` additionally ships a **read-only REST API fetch** that pulls the
model straight from the APIC into that file (data acquisition for ACI is
API-based — there is no manual CSV/YAML to prepare):

- **`aci_converter`** converts a
  [Cisco Application Centric Infrastructure (ACI)](https://www.cisco.com/c/en/us/solutions/data-center-virtualization/application-centric-infrastructure/index.html)
  fabric — obtained from the APIC over its **read-only REST API**
  (`fetch_from_apic`) — into **two** Network Sketcher diagrams: the physical **underlay**
  (spine/leaf/APIC + LLDP cabling) and the logical **overlay**
  (Tenant/VRF/BD/EPG + contracts).
- **`cml_converter`** converts a
  [Cisco Modeling Labs (CML)](https://developer.cisco.com/modeling-labs/) lab
  YAML file (plus any embedded running-configs) into a ready-to-run Network
  Sketcher command script — reconstructing L1/L2/L3.
- **`cv_converter`** turns
  [Cisco Cyber Vision](https://www.cisco.com/site/us/en/products/security/industrial-security/cyber-vision/index.html)
  asset (`networkNodes`) and `activities` CSV exports into an **OT topology laid
  out along the Purdue model / IEC 62443 / CPwE zones** (Enterprise → IDMZ →
  Industrial → Cell/Area).
- **`sna_converter`** turns a
  [Cisco Secure Network Analytics (SNA / Stealthwatch)](https://www.cisco.com/site/us/en/products/security/security-analytics/secure-network-analytics/index.html)
  NetFlow Flow Search CSV export into Network Sketcher commands **plus** a
  `[FLOW]` traffic matrix.

- **Technology stack:** Python standalone CLIs. `aci_converter`, `cv_converter`
  and `sna_converter` run on the Python standard library only (no pip packages;
  `aci_converter` needs 3.10+, `cv_converter` / `sna_converter` need 3.8+).
  `cml_converter` needs Python 3.10+ with `PyYAML` (and optionally
  `ciscoconfparse2`).
- **Status:** Actively developed. `aci_converter` converts an APIC model
  (fetched via the read-only REST API) into separate physical-underlay and
  logical-overlay diagrams — validated end-to-end against a live APIC and the
  Network Sketcher engine. `cml_converter` is at 1.0 — validated end-to-end
  against the live Network Sketcher engine and a corpus of 60+ public CML
  community labs. `cv_converter` maps Cyber Vision assets/flows onto an OT
  reference architecture; `sna_converter` derives a topology and traffic matrix
  from observed NetFlow. More tools are planned (see the table below).

---

## Tools in this extension

The extension is a monorepo: each tool lives in its own sub-directory with its
own `README.md` and `requirements.txt`. This top-level file gives a short
**Installation + Usage** summary per tool; the **full details** (use cases,
supported formats, option tables, known issues) live in each tool's folder
`README.md`. Available tools are listed alphabetically; new tools are added in
parallel as they become available.

| Product | Tool | What it does | Input | Status |
|---------|------|--------------|-------|--------|
| Application Centric Infrastructure (ACI) | [`aci_converter`](./aci_converter/) | Convert an APIC fabric into two diagrams: the physical **underlay** (spine/leaf/APIC) and the logical **overlay** (Tenant/VRF/BD/EPG + contracts). | APIC model via read-only REST API (`fetch_from_apic`) → JSON | ✅ Available |
| Cisco Modeling Labs (CML) | [`cml_converter`](./cml_converter/) | Convert a CML topology YAML (+ embedded running-configs) into Network Sketcher commands | CML lab YAML (local file) | ✅ Available |
| Cyber Vision | [`cv_converter`](./cv_converter/) | Build an OT topology (Purdue / IEC 62443 / CPwE zones) from Cyber Vision asset + activity exports. | Cisco Cyber Vision networkNodes + activities CSV (local files) | ✅ Available |
| Secure Network Analytics | [`sna_converter`](./sna_converter/) | Reconstruct a multi-site L1/L2/L3 topology + endpoints from observed NetFlow. | Cisco SNA (Secure Network Analytics) Flow Search CSV (local file) | ✅ Available |
| Catalyst Center | — | Catalyst Center (formerly DNA Center) → Network Sketcher | — | 📋 Planning |
| Catalyst SD-WAN | — | Catalyst SD-WAN (formerly Cisco SD-WAN / Viptela) → Network Sketcher | — | 📋 Planning |
| Nexus Dashboard | — | Nexus Dashboard → Network Sketcher | — | 📋 Planning |
| Secure Firewall Management Center | — | Secure Firewall Management Center (formerly Firepower Management Center / FMC) → Network Sketcher | — | 📋 Planning |

> Jump to a tool's section below for its installation and usage summary, then
> follow the link to that tool's folder `README.md` for full documentation.
> Every tool produces Network Sketcher CLI commands, so the
> ["Run the commands in Network Sketcher"](#running-the-output-in-network-sketcher)
> guidance at the end applies to all of them.

---

## Tool 1 — `aci_converter`

Convert a Cisco ACI fabric into Network Sketcher command scripts — both the
physical **underlay** (spine/leaf/border-leaf/APIC + observed LLDP cabling) and
the logical **overlay** (Tenant → VRF → Bridge Domain → EPG, with contracts as a
`[Flow_List]` traffic matrix). The fabric model is pulled from the APIC over its
**read-only REST API** (`fetch_from_apic`) — so there is no file to prepare by
hand. **Acquisition (API) and conversion (offline) are separate steps**, so one
fetch can drive many conversions.

<img width="2237" height="1181" alt="image" src="https://github.com/user-attachments/assets/04087f24-f6e1-4251-a222-2e127cd10bd4" />

<img width="1766" height="252" alt="image" src="https://github.com/user-attachments/assets/a7dde2cf-89e0-4356-a468-1a958716c18f" />

[sample_aci.zip](https://github.com/user-attachments/files/29280501/sample_aci.zip)

> **Full documentation** (the underlay/overlay model, what is observed vs.
> inferred, `fetch_from_apic` options including `--no-topology`, device/segment
> naming, colour conventions, accuracy caveats, known issues) is in
> [`aci_converter/README.md`](./aci_converter/).

### Installation

`aci_converter` has **no third-party dependencies** — it runs on the Python
3.10+ standard library:

```bash
git clone https://github.com/CiscoDevNet/network-sketcher-cisco-extension.git
cd network-sketcher-cisco-extension
pip install -r aci_converter/requirements.txt        # no-op (stdlib only)
```

### Usage

**Step 1 — fetch the model from the APIC over the read-only REST API** (the
password comes from the `ACI_PASSWORD` env var; by default it also pulls
operational `fabricNode` / `lldpAdjEp` / `fvCEp` for an accurate underlay and
real endpoints — add `--no-topology` for a config-only pull):

```bash
ACI_PASSWORD=... python -m aci_converter.src.fetch_from_apic \
    --host apic.example.com --user admin \
    --out aci_converter/Input_data/apic_export.json
```

**Step 2 — convert the JSON offline** (no APIC connection; repeatable):

```bash
python -m aci_converter.src.convert \
    -i aci_converter/Input_data/apic_export.json \
    -m both \
    -o aci_converter/Output_data/ns_commands.txt
```

This writes two command scripts — `ns_commands_aci_underlay(L1Only).txt`
(physical fabric) and `ns_commands_aci_overlay.txt` (logical policy) — plus
`gen_flow_list.csv` (contract traffic matrix), `ns_model_*.json`,
`aci_inventory.csv`, and `aci_underlay_report.md` / `aci_overlay_report.md`.
Example output:

```text
# underlay — spine/leaf CLOS from observed LLDP adjacencies
add l1_link_bulk "[['leaf-101','spine-201','Ethernet 1/49','Ethernet 1/1']]"
# overlay — EPG bound to its BD broadcast domain, gatewayed by the anycast SVI
add l2_segment_bulk "[['Web-BD-Shop-Web','Dummy 0',['Vlan110']]]"
```

Then run the commands in Network Sketcher — see
[Running the output in Network Sketcher](#running-the-output-in-network-sketcher).
The `gen_flow_list.csv` can additionally be pasted into the master's
`[Flow_List]` sheet.

---

## Tool 2 — `cml_converter`

Convert a Cisco Modeling Labs lab (topology YAML + any embedded running-configs)
into a ready-to-run Network Sketcher command script — reconstructing L1/L2/L3,
entirely from local files (no CML connectivity at conversion time).

<img width="1109" height="337" alt="image" src="https://github.com/user-attachments/assets/def702e3-8b6f-44ae-a961-6faeb8a35142" />

<img width="783" height="302" alt="image" src="https://github.com/user-attachments/assets/0211fcd4-42a4-4ad0-9d86-33512530494d" />

<img width="1227" height="588" alt="image" src="https://github.com/user-attachments/assets/43188e91-bb16-4391-ae0d-9c1cafedf5c6" />

[[L1L2L3_DIAGRAM]AllAreas_no_data_1.html](https://github.com/user-attachments/files/28463950/L1L2L3_DIAGRAM.AllAreas_no_data_1.html)

[[DEVICE_TABLE]no_data_1.html](https://github.com/user-attachments/files/28463946/DEVICE_TABLE.no_data_1.html)

> **Full documentation** (use cases, supported CML YAML formats, how Layer 2/L3
> reconstruction works, output file list, known issues) is in
> [`cml_converter/README.md`](./cml_converter/).

### Installation

```bash
git clone https://github.com/CiscoDevNet/network-sketcher-cisco-extension.git
cd network-sketcher-cisco-extension

# Python 3.10+ virtual environment
python -m venv venv && source venv/bin/activate     # Windows: .\venv\Scripts\Activate.ps1

pip install -r cml_converter/requirements.txt        # PyYAML (+ optional ciscoconfparse2)
```

### Usage

Export your lab from CML (**Lab → Export → Download Lab (YAML)**), then run the
converter:

```bash
python -m cml_converter.src.convert --yaml my_lab.yaml --out output/ns_commands.txt
```

Optionally pass running-configs separately with `--configs running_configs/`
(each file stem must match the CML node `label`, e.g. `spine1.txt` ↔ `spine1`).
This writes `ns_commands.txt` (the main deliverable) plus `ns_model.json`,
`stencil_mapping.csv` and `parse_report.md`. Example output:

```text
# Phase: 1 device_location
add device_location "['Campus',[['core1','core2'],['acc1','acc2']]]"
# Phase: 2 l1_link_bulk
add l1_link_bulk "[['core1','acc1','GigabitEthernet 0/1','GigabitEthernet 0/0']]"
# Phase: 4 ip_address_bulk
add ip_address_bulk "[['core1','Vlan 10',['10.0.10.1/24']]]"
```

Then run the commands in Network Sketcher — see
[Running the output in Network Sketcher](#running-the-output-in-network-sketcher).

---

## Tool 3 — `cv_converter`

Build an **OT network topology** from Cisco Cyber Vision asset (`networkNodes`)
and `activities` CSV exports, laid out along the **Purdue model / IEC 62443 /
CPwE zones** (Enterprise → IDMZ → Industrial → Cell/Area) — entirely from local
files. Unlike `sna_converter` (which *infers* office/enterprise sites from
NetFlow), Cyber Vision already classifies assets into Groups with Device Types,
so this tool *maps* that structure onto an OT reference architecture.

<img width="2353" height="304" alt="image" src="https://github.com/user-attachments/assets/a62a3e2e-e5b2-47a2-8484-3818b41a433a" />

<img width="2319" height="437" alt="image" src="https://github.com/user-attachments/assets/802a8111-33e6-43a7-85d6-b706c32da982" />

<img width="2482" height="239" alt="image" src="https://github.com/user-attachments/assets/43ba3f86-a86f-41cb-bd58-0579365d8760" />

[cv_sample_1.zip](https://github.com/user-attachments/files/29197721/cv_sample_1.zip)


> **Full documentation** (zone classification logic, CPwE infrastructure
> synthesis, ENT-Edge/IND-Edge access switches, IDMZ straddlers, attribute-sheet
> colours, the bandwidth caveat, known issues) is in
> [`cv_converter/README.md`](./cv_converter/).

### Installation

`cv_converter` has **no third-party dependencies** — it runs on the Python 3.8+
standard library:

```bash
pip install -r cv_converter/requirements.txt
```

### Usage

Export the **networkNodes** (assets) and **activities** (flows) lists from Cyber
Vision as CSV. Run from the tool's folder so the default `Input_data/` and
`Output_data/` folders resolve:

```bash
cd cv_converter

# auto-detect both CSVs dropped in Input_data/
python cv_to_ns_commands.py

# …or pass them explicitly
python cv_to_ns_commands.py --nodes networkNodes.csv --activities activities.csv --output-dir Output_data
```

This writes `gen_master_commands.txt` (the main deliverable) plus
`gen_flow_list.csv`, `gen_zone_assignment.csv` (review the zone decisions!),
`gen_conduit_report.csv` and `out_of_scope.csv`. Example output:

```text
# Phase: 1 area_location
add area_location "[['Industrial-Zone','Furnace']]"
# Phase: 1 device_location
add device_location "['Industrial-Zone',[['IND-Edge','','IND-Core']]]"
# Phase: 4 ip_address_bulk
add ip_address_bulk "[['LD810EP','Vlan 30',['192.168.30.11/24']]]"
```

Then run the commands in Network Sketcher — see
[Running the output in Network Sketcher](#running-the-output-in-network-sketcher).

> **Note on bandwidth:** `gen_flow_list.csv`'s `Max. bandwidth(Mbps)` uses the
> same formula as `sna_converter`, but Cyber Vision timestamps span the whole
> observation window, so the value is a long-term average (often ≈ 0), not a
> peak/session throughput. See the [folder README](./cv_converter/) for how to
> obtain meaningful bandwidth from Cyber Vision.

---

## Tool 4 — `sna_converter`

Reconstruct a multi-site L1/L2/L3 topology **plus endpoints** from observed
**traffic** instead of a device inventory: `sna_converter` reads a Cisco Secure
Network Analytics (SNA / Stealthwatch) NetFlow **Flow Search CSV** and produces
a Network Sketcher command script and a `[FLOW]` traffic matrix — entirely from a
local file, with no SNA server connection required.

<img width="1043" height="644" alt="image" src="https://github.com/user-attachments/assets/80d0f50d-1c37-41b5-a921-0f8da23bbb15" />

> **Full documentation** (site inference logic, endpoint naming conventions,
> supported CSV formats, the `Max. bandwidth(Mbps)` formula, `sna_to_ns_config.json`
> settings, known issues) is in [`sna_converter/README.md`](./sna_converter/).

### Installation

`sna_converter` has **no third-party dependencies** — it runs on the Python 3.8+
standard library (Windows, macOS, Linux):

```bash
pip install -r sna_converter/requirements.txt
```

### Usage

In the SNA Manager, run a **Flow Search** for the scope/time range of interest
and export the results to CSV (both the API and UI export formats are
auto-detected). Run from the tool's folder so the default `Input_data/` and
`Output_data/` folders resolve:

```bash
cd sna_converter

# convert every CSV in Input_data/ (a sample_flows.csv is included)
python sna_to_ns_commands.py

# …or a single CSV anywhere, or every CSV in a specific folder
python sna_to_ns_commands.py path/to/flowAnalysis.csv
```

For each input CSV, `Output_data/<csv_name>/` is created with
`gen_master_commands.txt` (the main deliverable), `gen_flow_list.csv` (the
`[FLOW]` paste sheet, with `Max. bandwidth(Mbps)`) and `out_of_scope_ips.csv`.
Endpoint generation (`--endpoints`), site grouping and detection thresholds are
all configured in `sna_to_ns_config.json`.

Then run the commands in Network Sketcher — see
[Running the output in Network Sketcher](#running-the-output-in-network-sketcher).
The `gen_flow_list.csv` can additionally be pasted into the master's
`[Flow_List]` sheet.

---

## Running the output in Network Sketcher

Every tool in this extension emits a plain-text `ns_commands.txt` script: one
Network Sketcher CLI command per line (lines starting with `#` are phase
comments and can be ignored). The commands are already in the correct
Phase 1 → 6 order, so run them top to bottom against a Network Sketcher master
file.

1. Install Network Sketcher by following the instructions in the
   [cisco-open/network-sketcher](https://github.com/cisco-open/network-sketcher)
   repository.
2. Create (or choose) an empty master file, e.g. `[MASTER]my_lab.nsm`. Starting
   from a freshly created empty master keeps Phase 1 device/area placement on a
   clean canvas.
3. Run the non-comment lines of `ns_commands.txt` in order against that master.
4. Export the diagram to get the L1/L2/L3 viewer and device table.

---

## Repository structure (monorepo)

```
network-sketcher-cisco-extension/
├── README.md           ← you are here
├── LICENSE             (Apache 2.0)
├── NOTICE
├── SECURITY.md
├── CODE_OF_CONDUCT.md
├── CONTRIBUTING.md
├── aci_converter/      ← Tool 1: APIC fabric (read-only REST API) → underlay + overlay NS commands
│   ├── README.md
│   ├── requirements.txt
│   ├── aci_to_ns_config.json    (settings: value / description / sample)
│   ├── Input_data/              (apic_export.json — fetched via the read-only REST API; includes sample_export.json)
│   ├── Output_data/             (ns_commands_aci_underlay(L1Only).txt, ns_commands_aci_overlay.txt, gen_flow_list.csv, …)
│   └── src/                     (convert.py + fetch_from_apic.py + mappers)
├── cml_converter/      ← Tool 2: CML YAML → NS commands
│   ├── README.md
│   ├── requirements.txt
│   ├── cml_to_ns_config.json    (settings: value / description)
│   └── src/
├── cv_converter/       ← Tool 3: Cyber Vision CSV → OT (Purdue/IEC 62443) NS commands
│   ├── README.md
│   ├── requirements.txt
│   ├── cv_to_ns_commands.py     (entry point)
│   ├── cv_to_ns_config.json     (settings: value / description / sample)
│   ├── Input_data/              (drop your CV networkNodes + activities CSVs here)
│   └── Output_data/             (gen_master_commands.txt, gen_flow_list.csv, …)
└── sna_converter/      ← Tool 4: SNA / NetFlow CSV → NS commands + [FLOW] matrix
    ├── README.md
    ├── requirements.txt
    ├── sna_to_ns_commands.py    (entry point)
    ├── sna_to_ns_config.json    (settings: value / description / sample)
    ├── Input_data/              (drop your SNA CSVs here; includes sample_flows.csv)
    └── Output_data/             (results, one subfolder per input CSV)
# future tools are added here as sibling sub-directories
```

Each tool is self-contained (its own `README.md` and `requirements.txt`) so it
can be installed and used independently of the others.

---

## Getting help

- Open an issue on the
  [GitHub issues](https://github.com/CiscoDevNet/network-sketcher-cisco-extension/issues)
  page describing the problem, the tool and input format you used, and the
  relevant report (`parse_report.md` for `cml_converter`; `aci_underlay_report.md`
  / `aci_overlay_report.md` for `aci_converter`).

## Getting involved

Contributions are welcome — new source converters, broader config coverage, and
additional Network Sketcher command support are all great areas to help with.
See [CONTRIBUTING.md](./CONTRIBUTING.md) for how to set up a dev environment and
submit changes. Please also review [CODE_OF_CONDUCT.md](./CODE_OF_CONDUCT.md)
and [SECURITY.md](./SECURITY.md).

## Credits and references

- [Network Sketcher](https://github.com/cisco-open/network-sketcher) — the
  open-source Cisco network documentation tool these extensions target.
- [Cisco Application Centric Infrastructure (ACI) / APIC](https://www.cisco.com/c/en/us/solutions/data-center-virtualization/application-centric-infrastructure/index.html)
  — the SDN data-center platform whose APIC read-only REST API
  feeds `aci_converter`.
- [Cisco Modeling Labs](https://developer.cisco.com/modeling-labs/) — the
  network simulation platform used as the data source for `cml_converter`.
- [Cisco Cyber Vision](https://www.cisco.com/site/us/en/products/security/industrial-security/cyber-vision/index.html)
  — the OT/ICS visibility platform whose asset + activity exports feed `cv_converter`.
- [Cisco Secure Network Analytics (SNA / Stealthwatch)](https://www.cisco.com/site/us/en/products/security/security-analytics/secure-network-analytics/index.html)
  — the NetFlow analytics platform whose Flow Search export feeds `sna_converter`.
- [CiscoDevNet/cml-community](https://github.com/CiscoDevNet/cml-community) —
  public CML labs used to validate the converter.

## Author

Yusuke Ogawa - Architect, Cisco | CCIE#17583

## License

This project is licensed under the [Apache License 2.0](./LICENSE). See the
[NOTICE](./NOTICE) file for copyright and third-party attributions.
