# Network Sketcher Cisco Extension — bridge Cisco platforms to automatic network diagrams

**Network Sketcher Cisco Extension** is a growing collection of tools that turn
Cisco platform data into
[Network Sketcher](https://github.com/cisco-open/network-sketcher) CLI commands,
so you can rebuild an accurate L1/L2/L3 topology — devices, links, VLANs, SVIs,
sub-interfaces, IP addressing and VRFs — in seconds instead of drawing it by
hand.

<img alt="image" src="https://github.com/user-attachments/assets/1dededa6-3d7d-4f7e-8606-c9dacd2684c1" />

<img alt="image" src="https://github.com/user-attachments/assets/d6d9573e-e183-4dcf-96c1-2531a3c18e2f" />


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
`meraki_converter`, `nd_converter` and `sdwan_converter` additionally ship a
**read-only REST API fetch** that pulls the model straight from the platform
into that file; the others read a file you export from the platform.

All tools are standalone Python CLIs. Most run on the Python standard library
alone — `cml_converter` needs `PyYAML`, `config_converter` and
`netbox_converter` need `networkx`; see each tool's folder `README.md` for its
exact Python version and dependencies.

---

## Tools in this extension

The extension is a monorepo: each tool lives in its own sub-directory with its
own `README.md` and `requirements.txt`. **The folder `README.md` linked from
each row below is the complete source of information for that tool** —
installation, usage, supported formats, options, sample diagrams, and known
issues. Tools are listed alphabetically.

| Product | Tool | What it does | Input | Status |
|---------|------|--------------|-------|--------|
| Application Centric Infrastructure (ACI) | [`aci_converter`](./aci_converter/) | Convert an APIC fabric into two diagrams: the physical **underlay** (spine/leaf/APIC) and the logical **overlay** (Tenant/VRF/BD/EPG + contracts). | APIC model via read-only REST API (`fetch_from_apic`) → JSON | ✅ Available |
| Catalyst Center | [`catc_converter`](./catc_converter/) | Convert an SD-Access campus into two diagrams: the physical **underlay** (core/distribution/access) and the logical **overlay** (Virtual Network / anycast gateway). | Catalyst Center model via read-only Intent REST API (`fetch_from_catc`) → JSON | ✅ Available |
| Catalyst SD-WAN | [`sdwan_converter`](./sdwan_converter/) | Convert a vManage-managed SD-WAN fabric into two diagrams: the physical **underlay** (VPN0 transport circuits above each Edge, its real Service VPN LAN ports below it, and the controllers attached to the transport their observed control connections use) and the Service VPN **overlay** (one waypoint cloud per VRF — e.g. Corporate / PCI / Guest — above each Edge, its real VRF LAN ports on a synthetic per-Edge LAN switch below it). | vManage model via read-only REST API (`fetch_from_vmanage`) → JSON | ✅ Available |
| Cisco Modeling Labs (CML) | [`cml_converter`](./cml_converter/) | Convert a CML topology YAML (+ embedded running-configs) into Network Sketcher commands | CML lab YAML (local file) | ✅ Available |
| Config files (IOS / IOS-XE / NX-OS / IOS-XR / ASA) | [`config_converter`](./config_converter/) | Reconstruct L1/L2/L3 from `show running-config` text files (subnet-based topology inference; no live device connection). | Running-config text files (local directory) | ✅ Available |
| Cyber Vision | [`cv_converter`](./cv_converter/) | Build an OT topology (Purdue / IEC 62443 / CPwE zones) from Cyber Vision asset + activity exports. | Cisco Cyber Vision networkNodes + activities CSV (local files) | ✅ Available |
| Meraki | [`meraki_converter`](./meraki_converter/) | Convert a Meraki organization into a Network Sketcher command script reconstructing L1/L2/L3. | Meraki org via read-only Dashboard API v1 (`fetch_from_meraki`) → JSON | ✅ Available |
| Nexus Dashboard | [`nd_converter`](./nd_converter/) | Convert an NDFC VXLAN EVPN fabric into two diagrams: the physical **underlay** (leaf/spine/border) and the logical **overlay** (VRF / Network / anycast gateway). | NDFC model via read-only REST API (`fetch_from_nd`) → JSON | ✅ Available |
| Secure Network Analytics | [`sna_converter`](./sna_converter/) | Reconstruct a multi-site L1/L2/L3 topology + endpoints from observed NetFlow. | Cisco SNA (Secure Network Analytics) Flow Search CSV (local file) | ✅ Available |

> Every tool produces Network Sketcher CLI commands, so the
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
├── aci_converter/       ← ACI fabric (REST API) → underlay + overlay
├── catc_converter/      ← Catalyst Center (REST API) → underlay + overlay
├── cml_converter/       ← CML YAML → L1/L2/L3
├── config_converter/    ← running-config text → L1/L2/L3 (inferred)
├── cv_converter/        ← Cyber Vision CSV → OT (Purdue / IEC 62443)
├── meraki_converter/    ← Meraki org (Dashboard API) → L1/L2/L3
├── nd_converter/        ← Nexus Dashboard / NDFC (REST API) → underlay + overlay
├── sdwan_converter/     ← Catalyst SD-WAN / vManage (REST API) → transport underlay + Service VPN overlay
├── sna_converter/       ← SNA / NetFlow CSV → commands + [FLOW] matrix
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
- Cisco IOS / IOS-XE / NX-OS / IOS-XR / ASA running-config exports — the
  local text files that feed `config_converter` (subnet-inferred topology; no
  platform API at conversion time).
- [Cisco Cyber Vision](https://www.cisco.com/site/us/en/products/security/industrial-security/cyber-vision/index.html)
  — the OT/ICS visibility platform whose asset + activity exports feed `cv_converter`.
- [Cisco Meraki](https://meraki.cisco.com/) — the cloud-managed networking
  platform whose read-only Dashboard API v1 feeds `meraki_converter`.
- [Cisco Nexus Dashboard](https://www.cisco.com/site/us/en/products/networking/cloud-networking/nexus-dashboard/index.html)
  — the NDFC / Fabric Controller platform whose read-only REST API feeds
  `nd_converter`.
- [Cisco Catalyst SD-WAN](https://www.cisco.com/site/us/en/products/networking/sd-wan/index.html)
  — the vManage-managed SD-WAN fabric platform whose read-only REST API feeds
  `sdwan_converter`.
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
