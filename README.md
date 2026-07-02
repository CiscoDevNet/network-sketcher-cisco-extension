# Network Sketcher Cisco Extension — bridge Cisco platforms to automatic network diagrams

**Network Sketcher Cisco Extension** is a growing collection of tools that turn
Cisco platform data into
[Network Sketcher](https://github.com/cisco-open/network-sketcher) CLI commands,
so you can rebuild an accurate L1/L2/L3 topology — devices, links, VLANs, SVIs,
sub-interfaces, IP addressing and VRFs — in seconds instead of drawing it by
hand.

> [!IMPORTANT]
> **The quality of each converted topology depends entirely on what data each
> Cisco product is able to provide** — whether obtained as a file export or
> through a read-only API fetch. Because the available data rarely contains a
> complete physical/logical inventory, the generated diagrams necessarily
> include a significant amount of **inference and synthesized structure**.
> Treat the output as a **starting reference / first draft only** — always
> review and validate it against authoritative sources before relying on it.
> Each converter has so far been validated against a limited number of
> samples (sandbox/demo environments); we expect validation against further
> real-world data to keep improving output quality.

Each tool targets a different Cisco data source, can be used independently, and
the **conversion runs entirely on local files** (no live platform connection is
needed at conversion time). Most tools read a file you export from the platform;
`aci_converter`, `catc_converter`, `meraki_converter` and `nd_converter`
additionally ship a **read-only REST API fetch** that pulls the model straight
from the platform into that file (data acquisition for those four is
API-based — there is no manual CSV/YAML to prepare):

- **`aci_converter`** converts a
  [Cisco Application Centric Infrastructure (ACI)](https://www.cisco.com/c/en/us/solutions/data-center-virtualization/application-centric-infrastructure/index.html)
  fabric — obtained from the APIC over its **read-only REST API**
  (`fetch_from_apic`) — into **two** Network Sketcher diagrams: the physical **underlay**
  (spine/leaf/APIC + LLDP cabling) and the logical **overlay**
  (Tenant/VRF/BD/EPG + contracts).
- **`catc_converter`** converts a
  [Cisco Catalyst Center](https://www.cisco.com/site/us/en/products/networking/wireless/catalyst-center/index.html)
  (formerly DNA Center) **SD-Access** campus — obtained over its **read-only
  Intent REST API** (`fetch_from_catc`) — into **two** Network Sketcher
  diagrams: the physical **underlay** (core/distribution/access + real
  observed cabling) and the logical **overlay** (Virtual Network / anycast
  gateway).
- **`cml_converter`** converts a
  [Cisco Modeling Labs (CML)](https://developer.cisco.com/modeling-labs/) lab
  YAML file (plus any embedded running-configs) into a ready-to-run Network
  Sketcher command script — reconstructing L1/L2/L3.
- **`cv_converter`** turns
  [Cisco Cyber Vision](https://www.cisco.com/site/us/en/products/security/industrial-security/cyber-vision/index.html)
  asset (`networkNodes`) and `activities` CSV exports into an **OT topology laid
  out along the Purdue model / IEC 62443 / CPwE zones** (Enterprise → IDMZ →
  Industrial → Cell/Area).
- **`meraki_converter`** converts a
  [Cisco Meraki](https://meraki.cisco.com/) organization — obtained over the
  **read-only Meraki Dashboard API v1** (`fetch_from_meraki`) — into a
  ready-to-run Network Sketcher command script reconstructing L1/L2/L3 (links,
  VLANs, SVIs, IPs, VRFs, HA/AutoVPN/LACP) under an `Internet` cloud waypoint.
- **`nd_converter`** converts a
  [Cisco Nexus Dashboard](https://www.cisco.com/site/us/en/products/networking/cloud-networking/nexus-dashboard/index.html)
  (NDFC / Fabric Controller) NX-OS VXLAN EVPN fabric — obtained over its
  **read-only REST API** (`fetch_from_nd`) — into **two** Network Sketcher
  diagrams: the physical **underlay** (leaf/spine/border + real observed
  cabling) and the logical **overlay** (VRF / Network / anycast gateway).
- **`sna_converter`** turns a
  [Cisco Secure Network Analytics (SNA / Stealthwatch)](https://www.cisco.com/site/us/en/products/security/security-analytics/secure-network-analytics/index.html)
  NetFlow Flow Search CSV export into Network Sketcher commands **plus** a
  `[FLOW]` traffic matrix.

- **Technology stack:** Python standalone CLIs. `aci_converter`,
  `catc_converter`, `cv_converter`, `meraki_converter`, `nd_converter` and
  `sna_converter` run on the Python standard library only (no pip packages;
  `aci_converter` / `catc_converter` / `nd_converter` need 3.10+, `cv_converter`
  / `meraki_converter` / `sna_converter` need 3.8+). `cml_converter` needs
  Python 3.10+ with `PyYAML` (and optionally `ciscoconfparse2`).
- **Status:** Actively developed. `aci_converter` converts an APIC model
  (fetched via the read-only REST API) into separate physical-underlay and
  logical-overlay diagrams — validated end-to-end against a live APIC and the
  Network Sketcher engine. `catc_converter` does the same for a live Catalyst
  Center SD-Access campus. `nd_converter` does the same and has been
  **validated end-to-end against a live Nexus Dashboard 4.1.1**.
  `cml_converter` is at 1.0 — validated end-to-end against the live Network
  Sketcher engine and a corpus of 60+ public CML community labs. `cv_converter`
  maps Cyber Vision assets/flows onto an OT reference architecture;
  `sna_converter` derives a topology and traffic matrix from observed NetFlow.
  `meraki_converter` has so far only been validated end-to-end against the
  **DevNet Reservable Meraki Sandbox**; several features remain unverified
  against a production org (see its
  [README](./meraki_converter/#verification-status-read-this-first)
  for the exact list). More tools are planned (see the table below).

---

## Tools in this extension

The extension is a monorepo: each tool lives in its own sub-directory with its
own `README.md` and `requirements.txt`. This top-level file gives a short
**Installation + Usage** summary per tool; the **full details** (use cases,
supported formats, option tables, known issues) live in each tool's folder
`README.md`. Tools are listed alphabetically; new tools are added in
parallel as they become available.

| Product | Tool | What it does | Input | Status |
|---------|------|--------------|-------|--------|
| Application Centric Infrastructure (ACI) | [`aci_converter`](./aci_converter/) | Convert an APIC fabric into two diagrams: the physical **underlay** (spine/leaf/APIC) and the logical **overlay** (Tenant/VRF/BD/EPG + contracts). | APIC model via read-only REST API (`fetch_from_apic`) → JSON | ✅ Available |
| Catalyst Center | [`catc_converter`](./catc_converter/) | Convert an SD-Access campus into two diagrams: the physical **underlay** (core/distribution/access) and the logical **overlay** (Virtual Network / anycast gateway). | Catalyst Center model via read-only Intent REST API (`fetch_from_catc`) → JSON | ✅ Available |
| Catalyst SD-WAN | — | Catalyst SD-WAN (formerly Cisco SD-WAN / Viptela) → Network Sketcher | — | 📋 Planning |
| Cisco Modeling Labs (CML) | [`cml_converter`](./cml_converter/) | Convert a CML topology YAML (+ embedded running-configs) into Network Sketcher commands | CML lab YAML (local file) | ✅ Available |
| Cyber Vision | [`cv_converter`](./cv_converter/) | Build an OT topology (Purdue / IEC 62443 / CPwE zones) from Cyber Vision asset + activity exports. | Cisco Cyber Vision networkNodes + activities CSV (local files) | ✅ Available |
| Meraki | [`meraki_converter`](./meraki_converter/) | Convert a Meraki organization into a Network Sketcher command script reconstructing L1/L2/L3. | Meraki org via read-only Dashboard API v1 (`fetch_from_meraki`) → JSON | ✅ Available |
| Nexus Dashboard | [`nd_converter`](./nd_converter/) | Convert an NDFC VXLAN EVPN fabric into two diagrams: the physical **underlay** (leaf/spine/border) and the logical **overlay** (VRF / Network / anycast gateway). | NDFC model via read-only REST API (`fetch_from_nd`) → JSON | ✅ Available |
| Secure Network Analytics | [`sna_converter`](./sna_converter/) | Reconstruct a multi-site L1/L2/L3 topology + endpoints from observed NetFlow. | Cisco SNA (Secure Network Analytics) Flow Search CSV (local file) | ✅ Available |

> Jump to a tool's section below for its installation and usage summary, then
> follow the link to that tool's folder `README.md` for full documentation.
> Every tool produces Network Sketcher CLI commands, so the
> ["Run the commands in Network Sketcher"](#running-the-output-in-network-sketcher)
> guidance at the end applies to all of them.

---

## Community / third-party converters

Tools that target a data source that is **not** a Cisco platform live under
[`3rd_party/`](./3rd_party/) instead of the repository root, so they stay
clearly separated from the Cisco-platform tools above. They follow the same
conventions (own `README.md` + `requirements.txt`, local-file conversion,
Network Sketcher CLI command output).

| Product | Tool | What it does | Input | Status |
|---------|------|--------------|-------|--------|
| NetBox | [`netbox_converter`](./3rd_party/netbox_converter/) | Convert a NetBox DCIM/IPAM instance into a Network Sketcher command script reconstructing L1/L2/L3, reusing the same placement logic as the Network Sketcher Offline edition's NetBox CSV import. | NetBox instance via read-only REST API (`fetch_from_netbox`) → JSON | ✅ Available |

### Installation

```bash
pip install -r 3rd_party/netbox_converter/requirements.txt   # networkx
```

### Usage

Because `3rd_party` cannot be a Python package name (it starts with a digit),
run these as modules **from the `3rd_party/` directory**:

```bash
cd 3rd_party

# 1. Fetch the instance over the read-only REST API
#    (token via the NETBOX_TOKEN env var)
NETBOX_TOKEN=... python -m netbox_converter.src.fetch_from_netbox \
    --url https://netbox.example.com \
    --out netbox_converter/Input_data/netbox_export.json

# 2. Convert offline (no NetBox connection)
python -m netbox_converter.src.convert \
    -i netbox_converter/Input_data/netbox_export.json \
    -o netbox_converter/Output_data/ns_commands_netbox.txt \
    -c netbox_converter/netbox_to_ns_config.json
```

See [`3rd_party/netbox_converter/README.md`](./3rd_party/netbox_converter/)
for the full documentation (placement/connection logic, config keys, data
coverage and known limitations).

---

## Tool 1 — `aci_converter`

Convert a Cisco ACI fabric into Network Sketcher command scripts — both the
physical **underlay** (spine/leaf/border-leaf/APIC + observed LLDP cabling) and
the logical **overlay** (Tenant → VRF → Bridge Domain → EPG, with contracts as a
`[Flow_List]` traffic matrix). The fabric model is pulled from the APIC over its
**read-only REST API** (`fetch_from_apic`) — so there is no file to prepare by
hand. **Acquisition (API) and conversion (offline) are separate steps**, so one
fetch can drive many conversions.

<img width="2237" height="1181" alt="aci1" src="https://github.com/user-attachments/assets/ad3b3274-cfeb-4dcf-b104-72aac5a7145a" />

<img width="1766" height="252" alt="aci2" src="https://github.com/user-attachments/assets/90c5298d-3cf9-441a-820f-27fa9ea92aa8" />


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

## Tool 2 — `catc_converter`

Convert a Cisco Catalyst Center (formerly DNA Center) SD-Access campus into
Network Sketcher command scripts — both the physical **underlay**
(core/distribution/access/WLCs/APs + real observed cabling) and the logical
**overlay** (Virtual Network → anycast-gateway segments, with SGT flows as an
opt-in `[Flow_List]` traffic matrix). The model is pulled from Catalyst Center
over its **read-only Intent REST API** (`fetch_from_catc`) — so there is no
file to prepare by hand. **Acquisition (API) and conversion (offline) are
separate steps**, so one fetch can drive many conversions.

> **Full documentation** (the underlay/overlay model, what is observed vs.
> synthesized, `fetch_from_catc` options, device/segment naming, colour
> conventions, accuracy caveats, known issues) is in
> [`catc_converter/README.md`](./catc_converter/).

### Installation

`catc_converter` has **no third-party dependencies** — it runs on the Python
3.10+ standard library:

```bash
pip install -r catc_converter/requirements.txt        # no-op (stdlib only)
```

### Usage

**Step 1 — fetch the model from Catalyst Center over the read-only REST API**
(the password comes from the `CATC_PASSWORD` env var):

```bash
CATC_PASSWORD=... python -m catc_converter.src.fetch_from_catc \
    --host catc.example.com --user admin \
    --out catc_converter/Input_data/catc_export.json
```

**Step 2 — convert the JSON offline** (no Catalyst Center connection;
repeatable):

```bash
python -m catc_converter.src.convert \
    -i catc_converter/Input_data/catc_export.json \
    -m both \
    -o catc_converter/Output_data/ns_commands.txt \
    -c catc_converter/catc_to_ns_config.json
```

This writes two command scripts — `ns_commands_catc_underlay(L1Only).txt`
(physical campus) and `ns_commands_catc_overlay.txt` (logical SD-Access) —
plus `gen_flow_list.csv` (SGT traffic matrix, opt-in), `ns_model_*.json`,
`catc_inventory.csv`, and `catc_underlay_report.md` / `catc_overlay_report.md`.
Example output:

```text
# underlay — real observed cabling from Catalyst Center's physical-topology
add l1_link_bulk "[['core-1','access-1','TenGigabitEthernet 1/0/1','GigabitEthernet 1/0/48']]"
# overlay — anycast-gateway segment, named after its reserved IP-pool
add l2_segment_bulk "[['Campus-Employees-Pool','Dummy 0',['Vlan110']]]"
```

Then run the commands in Network Sketcher — see
[Running the output in Network Sketcher](#running-the-output-in-network-sketcher).
The `gen_flow_list.csv` can additionally be pasted into the master's
`[Flow_List]` sheet if SGT flows were enabled.

---

## Tool 3 — `cml_converter`

Convert a Cisco Modeling Labs lab (topology YAML + any embedded running-configs)
into a ready-to-run Network Sketcher command script — reconstructing L1/L2/L3,
entirely from local files (no CML connectivity at conversion time).

<img width="1109" height="337" alt="image" src="https://github.com/user-attachments/assets/3fc59bf2-0ec9-43d1-986d-24dd63734c9c" />

<img width="783" height="302" alt="image" src="https://github.com/user-attachments/assets/114883b4-74b0-4a68-a6e6-264940148568" />

<img width="1227" height="588" alt="image" src="https://github.com/user-attachments/assets/4044d19d-332b-41d7-a34c-2329ccd34353" />


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

## Tool 4 — `cv_converter`

Build an **OT network topology** from Cisco Cyber Vision asset (`networkNodes`)
and `activities` CSV exports, laid out along the **Purdue model / IEC 62443 /
CPwE zones** (Enterprise → IDMZ → Industrial → Cell/Area) — entirely from local
files. Unlike `sna_converter` (which *infers* office/enterprise sites from
NetFlow), Cyber Vision already classifies assets into Groups with Device Types,
so this tool *maps* that structure onto an OT reference architecture.

<img width="2353" height="304" alt="image" src="https://github.com/user-attachments/assets/1add3423-2e36-44b4-bc80-f0e953253261" />

<img width="2319" height="437" alt="image" src="https://github.com/user-attachments/assets/61fa2a0c-7a26-413c-950a-ff1ec0c8bfd7" />

<img width="2482" height="239" alt="image" src="https://github.com/user-attachments/assets/5fe8fb63-9c5a-4b79-afb9-9f527e5100ad" />


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

## Tool 5 — `meraki_converter`

Convert a Cisco Meraki organization into a ready-to-run Network Sketcher
command script (L1/L2/L3) — no Meraki server access needed at conversion
time. Data acquisition is **API-based**: a read-only client pulls the org over
the **Meraki Dashboard API v1** into a JSON file, and the converter turns that
local file into a Network Sketcher command script, so the conversion is
reproducible offline (e.g. after a DevNet Sandbox reservation expires).

> [!NOTE]
> `meraki_converter` has so far only been validated end-to-end against the
> **DevNet Reservable Meraki Sandbox**. Several live-data code paths
> (LLDP/CDP discovery, switch SVIs, AutoVPN, LACP, …) fall back gracefully
> when their data is absent but remain unverified against a production org —
> see the "⚠️ Verification status" table in
> [`meraki_converter/README.md`](./meraki_converter/) for the exact list.

> **Full documentation** (verification status, device naming/colour
> conventions, `fetch_from_meraki` options, `meraki_to_ns_config.json`
> settings, known issues) is in
> [`meraki_converter/README.md`](./meraki_converter/).

### Installation

`meraki_converter` has **no third-party dependencies** — it runs on the Python
3.8+ standard library:

```bash
pip install -r meraki_converter/requirements.txt        # no-op (stdlib only)
```

### Usage

**Step 1 — get a read-only Meraki API key and the org id**, then fetch the org
over the read-only Dashboard API (the key comes from the `MERAKI_API_KEY` env
var):

```bash
export MERAKI_API_KEY=...          # Windows PowerShell: $env:MERAKI_API_KEY="..."

python -m meraki_converter.src.fetch_from_meraki \
    --org-id <ORG_ID> \
    --out    meraki_converter/Input_data/meraki_export.json
```

**Step 2 — convert the saved JSON** (no live API needed):

```bash
python -m meraki_converter.src.convert \
    -i meraki_converter/Input_data/meraki_export.json \
    -o meraki_converter/Output_data/ns_commands_meraki.txt \
    -c meraki_converter/meraki_to_ns_config.json
```

This writes `ns_commands_meraki.txt` (the main deliverable) plus
`ns_model_meraki.json`, `meraki_inventory.csv` and `meraki_report.md`. Example
output:

```text
# a real device keeps its inventory identity: <Model>_<last-4-of-serial>
add device_location "['Internet',[['MX100_YL5K']]]"
# single-LAN MX gateway SVI + IP from appliance/singleLan
add ip_address_bulk "[['MX100_YL5K','Vlan 1',['192.168.128.1/24']]]"
```

Then run the commands in Network Sketcher — see
[Running the output in Network Sketcher](#running-the-output-in-network-sketcher).

---

## Tool 6 — `nd_converter`

Convert a Cisco Nexus Dashboard (NDFC / Fabric Controller) NX-OS VXLAN EVPN
fabric into Network Sketcher command scripts — both the physical **underlay**
(leaf/spine/border/border-gateway + real observed cabling) and the logical
**overlay** (VRF → Network/L2VNI → anycast gateway, with intra-VRF
reachability as an opt-in `[Flow_List]` traffic matrix). The fabric model is
pulled from Nexus Dashboard over its **read-only REST API** (`fetch_from_nd`)
— so there is no file to prepare by hand. **Acquisition (API) and conversion
(offline) are separate steps**, so one fetch can drive many conversions.

> [!NOTE]
> Validated end-to-end against a live **Nexus Dashboard 4.1.1** (NDFC / Fabric
> Controller) and the Network Sketcher engine: both modes import cleanly and
> every L1/L2/L3 diagram artifact generates correctly.

> **Full documentation** (the underlay/overlay model, what is observed vs.
> synthesized, `fetch_from_nd` options, device/segment naming, colour
> conventions, accuracy caveats, known issues) is in
> [`nd_converter/README.md`](./nd_converter/).

### Installation

`nd_converter` has **no third-party dependencies** — it runs on the Python
3.10+ standard library:

```bash
pip install -r nd_converter/requirements.txt        # no-op (stdlib only)
```

### Usage

**Step 1 — fetch the model from Nexus Dashboard over the read-only REST API**
(the password comes from the `ND_PASSWORD` env var):

```bash
ND_PASSWORD=... python -m nd_converter.src.fetch_from_nd \
    --host nd.example.com --user admin \
    --out nd_converter/Input_data/nd_export.json
```

**Step 2 — convert the JSON offline** (no Nexus Dashboard connection;
repeatable):

```bash
python -m nd_converter.src.convert \
    -i nd_converter/Input_data/nd_export.json \
    -m both \
    -o nd_converter/Output_data/ns_commands.txt \
    -c nd_converter/nd_to_ns_config.json
```

This writes two command scripts — `ns_commands_nd_underlay(L1Only).txt`
(physical fabric) and `ns_commands_nd_overlay.txt` (logical VXLAN EVPN) —
plus `gen_flow_list.csv` (intra-VRF traffic matrix, opt-in), `ns_model_*.json`,
`nd_inventory.csv`, and `nd_underlay_report.md` / `nd_overlay_report.md`.
Example output:

```text
# underlay — real observed cabling from NDFC's control/links
add l1_link_bulk "[['site1-leaf1','site1-spine1','Ethernet 1/49','Ethernet 1/1']]"
# overlay — Network (L2VNI) bound to its VRF gateway's anycast SVI
add l2_segment_bulk "[['NET:RED_Web','Dummy 0',['Vlan30']]]"
```

Then run the commands in Network Sketcher — see
[Running the output in Network Sketcher](#running-the-output-in-network-sketcher).
The `gen_flow_list.csv` can additionally be pasted into the master's
`[Flow_List]` sheet if intra-VRF flows were enabled.

---

## Tool 7 — `sna_converter`

Reconstruct a multi-site L1/L2/L3 topology **plus endpoints** from observed
**traffic** instead of a device inventory: `sna_converter` reads a Cisco Secure
Network Analytics (SNA / Stealthwatch) NetFlow **Flow Search CSV** and produces
a Network Sketcher command script and a `[FLOW]` traffic matrix — entirely from a
local file, with no SNA server connection required.

<img width="1043" height="644" alt="image" src="https://github.com/user-attachments/assets/d2cb5e45-4eaa-44fb-869d-8527f93b8ea6" />


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
│   └── Output_data/             (ns_commands_aci_underlay(L1Only).txt, ns_commands_aci_overlay.txt, gen_flow_list.csv, …)
├── catc_converter/     ← Tool 2: Catalyst Center (read-only REST API) → underlay + overlay NS commands
│   ├── README.md
│   ├── requirements.txt
│   ├── catc_to_ns_config.json   (settings: value / description / sample)
│   ├── Input_data/              (catc_export.json — fetched via the read-only REST API; includes sample_catc_export.json)
│   └── Output_data/             (ns_commands_catc_underlay(L1Only).txt, ns_commands_catc_overlay.txt, gen_flow_list.csv, …)
├── cml_converter/      ← Tool 3: CML YAML → NS commands
│   ├── README.md
│   ├── requirements.txt
│   └── cml_to_ns_config.json    (settings: value / description)
├── cv_converter/       ← Tool 4: Cyber Vision CSV → OT (Purdue/IEC 62443) NS commands
│   ├── README.md
│   ├── requirements.txt
│   ├── cv_to_ns_commands.py     (entry point)
│   ├── cv_to_ns_config.json     (settings: value / description / sample)
│   ├── Input_data/              (drop your CV networkNodes + activities CSVs here; includes sample_networkNodes.csv + sample_activities.csv)
│   └── Output_data/             (gen_master_commands.txt, gen_flow_list.csv, …)
├── meraki_converter/   ← Tool 5: Meraki org (read-only Dashboard API v1) → NS commands
│   ├── README.md
│   ├── requirements.txt
│   ├── meraki_to_ns_config.json (settings: value / description / sample)
│   ├── Input_data/              (meraki_export.json — fetched via the read-only Dashboard API)
│   └── Output_data/             (ns_commands_meraki.txt, ns_model_meraki.json, …)
├── nd_converter/       ← Tool 6: Nexus Dashboard / NDFC (read-only REST API) → underlay + overlay NS commands
│   ├── README.md
│   ├── requirements.txt
│   ├── nd_to_ns_config.json     (settings: value / description / sample)
│   ├── Input_data/              (nd_export.json — fetched via the read-only REST API; includes sample_nd_export.json)
│   └── Output_data/             (ns_commands_nd_underlay(L1Only).txt, ns_commands_nd_overlay.txt, gen_flow_list.csv, …)
├── sna_converter/      ← Tool 7: SNA / NetFlow CSV → NS commands + [FLOW] matrix
│   ├── README.md
│   ├── requirements.txt
│   ├── sna_to_ns_commands.py    (entry point)
│   ├── sna_to_ns_config.json    (settings: value / description / sample)
│   ├── Input_data/              (drop your SNA CSVs here; includes sample_flows.csv)
│   └── Output_data/             (results, one subfolder per input CSV)
└── 3rd_party/          ← Community / third-party converters (non-Cisco platforms)
    └── netbox_converter/  ← NetBox (read-only REST API) → NS commands
        ├── README.md
        ├── requirements.txt
        ├── netbox_to_ns_config.json  (settings: value / description / sample)
        ├── Input_data/           (netbox_export.json — fetched via the read-only REST API)
        └── Output_data/          (ns_commands_netbox.txt, ns_model_netbox.json, …)
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
  / `aci_overlay_report.md` for `aci_converter`; `catc_underlay_report.md` /
  `catc_overlay_report.md` for `catc_converter`; `nd_underlay_report.md` /
  `nd_overlay_report.md` for `nd_converter`; `meraki_report.md` for
  `meraki_converter`).

## Credits and references

- [Network Sketcher](https://github.com/cisco-open/network-sketcher) — the
  open-source Cisco network documentation tool these extensions target.
- [Cisco Application Centric Infrastructure (ACI) / APIC](https://www.cisco.com/c/en/us/solutions/data-center-virtualization/application-centric-infrastructure/index.html)
  — the SDN data-center platform whose APIC read-only REST API
  feeds `aci_converter`.
- [Cisco Catalyst Center](https://www.cisco.com/site/us/en/products/networking/wireless/catalyst-center/index.html)
  — the SD-Access campus controller whose read-only Intent REST API feeds
  `catc_converter`.
- [Cisco Modeling Labs](https://developer.cisco.com/modeling-labs/) — the
  network simulation platform used as the data source for `cml_converter`.
- [Cisco Cyber Vision](https://www.cisco.com/site/us/en/products/security/industrial-security/cyber-vision/index.html)
  — the OT/ICS visibility platform whose asset + activity exports feed `cv_converter`.
- [Cisco Meraki](https://meraki.cisco.com/) — the cloud-managed networking
  platform whose read-only Dashboard API v1 feeds `meraki_converter`.
- [Cisco Nexus Dashboard](https://www.cisco.com/site/us/en/products/networking/cloud-networking/nexus-dashboard/index.html)
  — the NDFC / Fabric Controller platform whose read-only REST API feeds
  `nd_converter`.
- [Cisco Secure Network Analytics (SNA / Stealthwatch)](https://www.cisco.com/site/us/en/products/security/security-analytics/secure-network-analytics/index.html)
  — the NetFlow analytics platform whose Flow Search export feeds `sna_converter`.
- [CiscoDevNet/cml-community](https://github.com/CiscoDevNet/cml-community) —
  public CML labs used to validate the converter.
- [NetBox](https://netbox.dev/) — the open-source DCIM/IPAM platform whose
  read-only REST API feeds the community `netbox_converter`
  ([3rd_party/](./3rd_party/)).

## Author

Yusuke Ogawa - Architect, Cisco | CCIE#17583

## License

This project is licensed under the [Apache License 2.0](./LICENSE). See the
[NOTICE](./NOTICE) file for copyright and third-party attributions.
