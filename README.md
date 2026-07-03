# Network Sketcher Cisco Extension — bridge Cisco platforms to automatic network diagrams

**Network Sketcher Cisco Extension** is a growing collection of tools that turn
Cisco platform data into
[Network Sketcher](https://github.com/cisco-open/network-sketcher) CLI commands,
so you can rebuild an accurate L1/L2/L3 topology — devices, links, VLANs, SVIs,
sub-interfaces, IP addressing and VRFs — in seconds instead of drawing it by
hand.

<img width="1335" height="704" alt="image" src="https://github.com/user-attachments/assets/1dededa6-3d7d-4f7e-8606-c9dacd2684c1" />


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
> A feature to consolidate the individual NSM master files each converter/mode
> currently requires into a single unified master is still only at the
> planning stage — it is not yet decided or scheduled for implementation.

Each tool targets a different data source, can be used independently, and the
**conversion runs entirely on local files** (no live platform connection is
needed at conversion time). `aci_converter`, `catc_converter`,
`meraki_converter` and `nd_converter` additionally ship a **read-only REST API
fetch** that pulls the model straight from the platform into that file; the
others read a file you export from the platform.

All tools are standalone Python CLIs. Most run on the Python standard library
alone — `cml_converter` needs `PyYAML` and `netbox_converter` needs `networkx`;
see each tool's folder `README.md` for its exact Python version and
dependencies.

---

## Tools in this extension

The extension is a monorepo: each tool lives in its own sub-directory with its
own `README.md` and `requirements.txt`. **The folder `README.md` holds the full
details** — installation, usage, supported formats, options, and known issues.
The per-tool sections further down are only a short visual preview that links
to each. Tools are listed alphabetically.

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

> Each tool's section below shows a preview and links to its folder `README.md`
> for installation and usage. Every tool produces Network Sketcher CLI
> commands, so the
> ["Running the output in Network Sketcher"](#running-the-output-in-network-sketcher)
> guidance applies to all of them.

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

---

## `template_converter` — scaffold for building new converters

`template_converter/` is not a converter itself — it is a copy-paste
starting point for building a **new** "Platform X → Network Sketcher"
converter (Cisco or third-party) when this repository's toolset needs to be
extended. It documents the architecture every converter here shares (which
files are copy-verbatim vs. platform-specific), the credential/security
conventions, and the README conventions used repo-wide.

It is written primarily for **AI coding agents** to read and follow
directly — every converter in this repo was itself built by an AI agent —
though it works equally well as a reference for a human contributor. The
command-generation logic it documents (`ns_command_builder.py`'s Phase 1–6
ordering and the numbered `RULE N` constraints referenced throughout the
shared code) is grounded directly in Network Sketcher's own **AI Context**
export — the `[AI_Context]<master>.txt` file produced by the
`network-sketcher` MCP server (`get_ai_context` / `build_default_outputs`),
which documents the full CLI command reference and syntax rules.

See [`template_converter/GUIDE.md`](./template_converter/) for the full
authoring guide.

---

## Tool 1 — `aci_converter`

Convert a Cisco ACI fabric into two Network Sketcher diagrams — the physical
**underlay** (spine/leaf/border-leaf/APIC + observed LLDP cabling) and the
logical **overlay** (Tenant → VRF → Bridge Domain → EPG, with contracts as a
`[Flow_List]` traffic matrix) — from a model pulled over the APIC's
**read-only REST API** (`fetch_from_apic`).

<img width="2237" height="1181" alt="aci1" src="https://github.com/user-attachments/assets/ad3b3274-cfeb-4dcf-b104-72aac5a7145a" />

<img width="1766" height="252" alt="aci2" src="https://github.com/user-attachments/assets/90c5298d-3cf9-441a-820f-27fa9ea92aa8" />


[sample_aci.zip](https://github.com/user-attachments/files/29280501/sample_aci.zip)

> **Full documentation** (installation, usage, the underlay/overlay model,
> options, colour conventions, accuracy caveats, known issues) is in
> [`aci_converter/README.md`](./aci_converter/).

---

## Tool 2 — `catc_converter`

Convert a Cisco Catalyst Center (formerly DNA Center) SD-Access campus into two
Network Sketcher diagrams — the physical **underlay**
(core/distribution/access/WLCs/APs + real observed cabling) and the logical
**overlay** (Virtual Network → anycast-gateway segments) — from a model pulled
over its **read-only Intent REST API** (`fetch_from_catc`).

<img width="907" height="555" alt="image" src="https://github.com/user-attachments/assets/1ca7d68c-6c8e-4007-a7bb-a3f988fe3121" />


> **Full documentation** (installation, usage, the underlay/overlay model,
> options, colour conventions, accuracy caveats, known issues) is in
> [`catc_converter/README.md`](./catc_converter/).

---

## Tool 3 — `cml_converter`

Convert a Cisco Modeling Labs lab (topology YAML + any embedded
running-configs) into a ready-to-run Network Sketcher command script,
reconstructing L1/L2/L3 entirely from local files (no CML connectivity at
conversion time).

<img width="1109" height="337" alt="image" src="https://github.com/user-attachments/assets/3fc59bf2-0ec9-43d1-986d-24dd63734c9c" />

<img width="783" height="302" alt="image" src="https://github.com/user-attachments/assets/114883b4-74b0-4a68-a6e6-264940148568" />

<img width="1227" height="588" alt="image" src="https://github.com/user-attachments/assets/4044d19d-332b-41d7-a34c-2329ccd34353" />


[[L1L2L3_DIAGRAM]AllAreas_no_data_1.html](https://github.com/user-attachments/files/28463950/L1L2L3_DIAGRAM.AllAreas_no_data_1.html)

[[DEVICE_TABLE]no_data_1.html](https://github.com/user-attachments/files/28463946/DEVICE_TABLE.no_data_1.html)

> **Full documentation** (installation, usage, supported CML YAML formats, how
> L2/L3 reconstruction works, output file list, known issues) is in
> [`cml_converter/README.md`](./cml_converter/).

---

## Tool 4 — `cv_converter`

Build an **OT network topology** from Cisco Cyber Vision asset (`networkNodes`)
and `activities` CSV exports, laid out along the **Purdue model / IEC 62443 /
CPwE zones** (Enterprise → IDMZ → Industrial → Cell/Area) — entirely from local
files.

<img width="2353" height="304" alt="image" src="https://github.com/user-attachments/assets/1add3423-2e36-44b4-bc80-f0e953253261" />

<img width="2319" height="437" alt="image" src="https://github.com/user-attachments/assets/61fa2a0c-7a26-413c-950a-ff1ec0c8bfd7" />

<img width="2482" height="239" alt="image" src="https://github.com/user-attachments/assets/5fe8fb63-9c5a-4b79-afb9-9f527e5100ad" />


[cv_sample_1.zip](https://github.com/user-attachments/files/29197721/cv_sample_1.zip)


> **Full documentation** (installation, usage, zone classification logic, CPwE
> infrastructure synthesis, attribute-sheet colours, the bandwidth caveat,
> known issues) is in [`cv_converter/README.md`](./cv_converter/).

---

## Tool 5 — `meraki_converter`

Convert a Cisco Meraki organization into a ready-to-run Network Sketcher
command script (L1/L2/L3) — from a model pulled over the **read-only Meraki
Dashboard API v1** (`fetch_from_meraki`), so the conversion is reproducible
offline (e.g. after a DevNet Sandbox reservation expires).

<img width="1266" height="666" alt="image" src="https://github.com/user-attachments/assets/8ec8110b-2896-4140-a1bc-3b12462abdd5" />

> [!NOTE]
> Validated end-to-end only against the **DevNet Reservable Meraki Sandbox**;
> several live-data paths remain unverified against a production org — see the
> "⚠️ Verification status" table in the folder README.

> **Full documentation** (installation, usage, verification status, device
> naming/colour conventions, options, known issues) is in
> [`meraki_converter/README.md`](./meraki_converter/).

---

## Tool 6 — `nd_converter`

Convert a Cisco Nexus Dashboard (NDFC / Fabric Controller) NX-OS VXLAN EVPN
fabric into two Network Sketcher diagrams — the physical **underlay**
(leaf/spine/border/border-gateway + real observed cabling) and the logical
**overlay** (VRF → Network/L2VNI → anycast gateway) — from a model pulled over
its **read-only REST API** (`fetch_from_nd`).

<img width="1279" height="633" alt="image" src="https://github.com/user-attachments/assets/fa648322-e112-4bda-8508-9cbc42dd2870" />

> [!NOTE]
> Validated end-to-end against a live **Nexus Dashboard 4.1.1** (NDFC / Fabric
> Controller) and the Network Sketcher engine.

> **Full documentation** (installation, usage, the underlay/overlay model,
> options, colour conventions, accuracy caveats, known issues) is in
> [`nd_converter/README.md`](./nd_converter/).

---

## Tool 7 — `sna_converter`

Reconstruct a multi-site L1/L2/L3 topology **plus endpoints** from observed
**traffic** instead of a device inventory: `sna_converter` reads a Cisco Secure
Network Analytics (SNA / Stealthwatch) NetFlow **Flow Search CSV** and produces
a Network Sketcher command script plus a `[FLOW]` traffic matrix — entirely
from a local file.

<img width="1043" height="644" alt="image" src="https://github.com/user-attachments/assets/d2cb5e45-4eaa-44fb-869d-8527f93b8ea6" />


> **Full documentation** (installation, usage, site inference logic, endpoint
> naming conventions, supported CSV formats, the `Max. bandwidth(Mbps)`
> formula, known issues) is in [`sna_converter/README.md`](./sna_converter/).

---

## 3rd Party Tool 1 — `netbox_converter`

Convert a NetBox DCIM/IPAM instance into a ready-to-run Network Sketcher
command script reconstructing L1/L2/L3 — from a model pulled over its
**read-only REST API** (`fetch_from_netbox`).

<img width="963" height="531" alt="image" src="https://github.com/user-attachments/assets/8945d126-c673-4195-87cf-fec4f7c9dcbd" />

> **Full documentation** (installation, usage, placement/connection logic,
> config keys, data coverage and known limitations) is in
> [`3rd_party/netbox_converter/README.md`](./3rd_party/netbox_converter/).

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
├── README.md            ← you are here
├── LICENSE / NOTICE / SECURITY.md / CODE_OF_CONDUCT.md / CONTRIBUTING.md
├── aci_converter/       ← Tool 1: ACI fabric (REST API) → underlay + overlay
├── catc_converter/      ← Tool 2: Catalyst Center (REST API) → underlay + overlay
├── cml_converter/       ← Tool 3: CML YAML → L1/L2/L3
├── cv_converter/        ← Tool 4: Cyber Vision CSV → OT (Purdue / IEC 62443)
├── meraki_converter/    ← Tool 5: Meraki org (Dashboard API) → L1/L2/L3
├── nd_converter/        ← Tool 6: Nexus Dashboard / NDFC (REST API) → underlay + overlay
├── sna_converter/       ← Tool 7: SNA / NetFlow CSV → commands + [FLOW] matrix
├── template_converter/  ← scaffold for building new converters (contributor tooling)
└── 3rd_party/           ← community / third-party converters (non-Cisco platforms)
    └── netbox_converter/  ← NetBox (REST API) → L1/L2/L3
```

Each converter folder is self-contained — its own `README.md`,
`requirements.txt`, a `<tool>_to_ns_config.json`, and `Input_data/` +
`Output_data/` directories — so it can be installed and used independently of
the others.

---

## Getting help

- Open an issue on the
  [GitHub issues](https://github.com/CiscoDevNet/network-sketcher-cisco-extension/issues)
  page describing the problem, the tool and input format you used, and attach
  the relevant report file (each converter writes one — its name is given in
  the tool's folder `README.md`).

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
