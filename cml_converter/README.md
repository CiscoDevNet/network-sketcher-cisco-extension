# cml_converter — CML YAML to Network Sketcher Command Converter

Convert a [Cisco Modeling Labs (CML)](https://developer.cisco.com/modeling-labs/)
topology YAML file into a ready-to-run
[Network Sketcher](https://github.com/cisco-open/network-sketcher) command
script. Conversion reads a local file. An optional fetch step can pull
labs from a CML controller first.

> The Network Sketcher Offline edition has a built-in CML import, but it covers
> **Layer 1 only** (devices and physical links). `cml_converter` is the
> **extended version that also reconstructs Layer 2 and Layer 3** (VLANs, SVIs,
> sub-interfaces, port-channels / IOS-XR Bundle-Ether, IP addresses and VRFs)
> from the running-configs. IOS-XR running-configs (`ipv4 address`, `bundle id`,
> `MgmtEth`, per-interface `vrf`) are parsed to the same L2/L3 facts as
> `config_converter`; LAG names stay `Bundle-Ether N` (not rewritten to
> `Port-channel N`). Interface `ipv6 address <addr>/<len>` lines are drawn next
> to the IPv4 address (dual stack); link-local, EUI-64, `autoconfig` and `dhcp`
> lines are skipped, and IPv6 Network Sketcher rejects is listed in
> `parse_report.md`.

<img alt="CML lab converted into a Network Sketcher L1 diagram" src="https://github.com/user-attachments/assets/3fc59bf2-0ec9-43d1-986d-24dd63734c9c" />

<img alt="CML lab converted into a Network Sketcher L2 diagram" src="https://github.com/user-attachments/assets/114883b4-74b0-4a68-a6e6-264940148568" />

<img alt="CML lab converted into a Network Sketcher L3 diagram" src="https://github.com/user-attachments/assets/4044d19d-332b-41d7-a34c-2329ccd34353" />

[[L1L2L3_DIAGRAM]AllAreas_no_data_1.html](https://github.com/user-attachments/files/28463950/L1L2L3_DIAGRAM.AllAreas_no_data_1.html)

[[DEVICE_TABLE]no_data_1.html](https://github.com/user-attachments/files/28463946/DEVICE_TABLE.no_data_1.html)

---

## Overview

| Item | Detail |
|------|--------|
| **Input** | CML Lab YAML, or the multi-lab JSON from `fetch_from_cml`, plus optional running-config files |
| **Output** | `ns_commands.txt` ready for Network Sketcher `run_commands`, plus debug/audit artefacts |
| **Dependencies** | Python 3.10+, PyYAML — fetch and running-config parsing are stdlib-only |
| **CML connectivity** | Optional. `fetch_from_cml` is a read-only REST client; `convert` reads the saved file |
| **Release** | Ver 0.5.1b |

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Export your lab from the CML UI: Lab → Export → YAML
#    Save it as my_lab.yaml

# 3. Run the converter (run from the repository root)
python -m cml_converter.src.convert \
    --yaml  my_lab.yaml \
    --out   output/ns_commands.txt

# 4. Optional: also provide running-config files
python -m cml_converter.src.convert \
    --yaml    my_lab.yaml \
    --configs running_configs/ \
    --out     output/ns_commands.txt

# 5. Optional: pull every lab from a CML controller, then convert.
#    An administrator account also includes labs owned by other users.
#    Password from --password or the CML_PASSWORD environment variable.
#    TLS verification is off by default; pass --verify-tls to enforce it.
python -m cml_converter.src.fetch_from_cml \
    --host cml.example.com --user administrator \
    --out  cml_export.json
python -m cml_converter.src.convert \
    --yaml cml_export.json \
    --out  output/ns_commands.txt

# 6. Optional: draw the overlay (VXLAN EVPN / SD-WAN / L3VPN) found in the
#    configs as one integrated script (output/ns_commands_cml_overlay.txt).
#    --mode both writes the underlay and the overlay.
python -m cml_converter.src.convert \
    --yaml my_lab.yaml \
    --out  output/ns_commands.txt \
    --mode overlay
```

## Overlay mode

`--mode` chooses what is written:

| `--mode` | Output |
|----------|--------|
| `underlay` (default) | The physical L1/L2/L3 script, exactly as before |
| `overlay` | One integrated overlay script, `<stem>_cml_overlay.txt` |
| `both` | Both scripts |

The overlay is read from each node's saved config, from a YAML export or a
`fetch_from_cml` JSON alike. Three overlays are recognised and drawn together
in the same areas:

| Overlay | Recognised from | Drawn as (same style as) |
|---------|-----------------|--------------------------|
| **VXLAN EVPN** | NX-OS `vlan` / `vn-segment`, `vrf context` / `vni`, `interface nve1` / `member vni`; IOS-XE `vlan configuration` / `member evpn-instance … vni`, `member vni … vrf` | `VRF-GW:<area>-<vrf>` (distributed anycast gateway with the SVI IPs), `NET:<area>-<vlan name>` per L2VNI, and the hosts on leaf access ports (`nd_converter`). The leaves themselves are not drawn |
| **SD-WAN** | cEdge `system` (`system-ip`, `site-id`), the `sdwan` stanza's `tunnel-interface` ports or `tunnel mode sdwan`, numbered `vrf definition` Service VPNs | One waypoint cloud `VPN:<lab>-<id>` per Service VPN, linked from a synthetic `Vpn <id>` port on each Edge; real Service VPN ports face the LAN (`sdwan_converter`) |
| **L3VPN** | `router bgp` with `address-family vpnv4` / `vpnv6` and a VRF on a port; transport LDP / SR-MPLS / SRv6 named in the Attribute | One waypoint cloud `VRF:<lab>-<name>` per VRF (RD / route targets in its Attribute), linked from a synthetic `Vrf <n>` port on each PE; the PE's VRF ports link to their CEs |

`--overlay {auto,evpn,sdwan,l3vpn}` (repeatable) narrows the types drawn;
`auto` (default) draws every type found. ACI is not available from a CML lab.

**Areas.** Each SD-WAN site is one area, `<lab>-site<site-id>`. Every other
node joins the site of its nearest Edge over CML links on the LAN side; the
search never leaves an Edge through a WAN transport port, and a tie goes to the
smaller site-id. A lab without SD-WAN, and any node no Edge reaches, uses the
area `<lab>` (the lab title, or the YAML file name). The VPN and VRF clouds sit
in `<lab>-wan_wp_`, on its own row above that lab's sites. Inside an area the
rows are, top to bottom: Edge / PE, VRF-GW, NET / LAN switch, hosts / CE.

**Handoffs.** A CML link whose two ends are overlay ports (for example a
border leaf `vrf member BLUE` port cabled to a cEdge Service VPN 10 port) is
drawn as one link between the VRF-GW and the Edge, with the IP and VRF of each
real port. A CML unmanaged switch (or a switch with no config) in between is
drawn, and every node cabled to it is resolved the same way. A CE / VRF-lite
router on the LAN side is drawn with its own IP and VRF. Each pairing is
written to Attribute-E (`Handoff: …`) and to `overlay_report.md`:
`confirmed` when the two ports share a subnet, `inferred` otherwise. A link
that would join two different site areas is not drawn (Network Sketcher
allows no direct link between two non-waypoint areas); it is listed in the
report. A Service VPN port with no CML link goes to a gray `LAN:<edge>` dummy
switch, as in `sdwan_converter`. A VRF / VPN port on a `Bundle-Ether` or
`Port-channel` is followed through its member ports: the member carries the
link, and the IP and VRF stay on the logical port, as in the underlay.

**Overlay and underlay in one script.** Only a lab with an overlay is drawn as
an overlay; the rest stays as in the underlay:

- In a lab with an overlay, the devices that take no part in it (P routers,
  SD-WAN controllers and transport switches, External Connectors) are drawn
  with their underlay ports, addresses and colours in the same areas, and
  every underlay link between two drawn devices is added, so the overlay
  devices stay connected to the rest of the lab. An EVPN leaf is not drawn
  (its VRF-GW stands for it), so its links to the spine are left out.
- A lab with no overlay is copied from the underlay: its areas, device grid,
  links, L2 / L3, port-channels and External Connector waypoint areas.
- Labs are arranged as in the underlay; an overlay lab takes the place of its
  underlay area, with its WAN waypoint row above. Areas are joined the way the
  underlay joins them (through External Connector waypoints only); a link that
  would join two areas directly, such as a cEdge transport port to a transport
  switch in the lab area, is not drawn and is listed in `overlay_report.md`.
- Port Speed / Duplex / Type are `Unknown` on the overlay devices and the
  underlay values on the devices drawn as in the underlay.

**No overlay.** When the configs hold none of the three (or none of the
`--overlay` types), `--mode overlay` prints
`[ERROR] No overlay configuration (VXLAN EVPN / SD-WAN / L3VPN) was found in the node configs.`
and exits with code **3**, listing the nodes that still carry CML's day-0
config (run **Extract Configs** in CML and convert again). `--mode both` prints
the same message as a warning, writes the underlay, and exits 0.

## Fetching labs from CML

`fetch_from_cml` authenticates with `POST /api/v0/authenticate` and downloads
each lab with `GET /api/v0/labs/{id}` and `GET /api/v0/labs/{id}/topology`.
It never starts, stops, or edits a lab. The password is not written to the
export or to the log. Each node's pyATS block (`username`, `password`,
`enable_password`) is removed before the file is written. Each node's saved
configuration is kept, because `convert` reads it, so the export can still
contain the `enable secret`, `username ... password` and SNMP community lines
of those configs; treat it like the configs themselves.

The export carries each node's **saved** configuration, not the running one.
A node that was never saved in CML still holds CML's day-0 template
(`hostname inserthostname-here`); `convert` skips it and lists it in
`parse_report.md` as "config not extracted". To include those nodes, run
**Extract Configs** on the lab in CML before fetching.

The lab list is `GET /api/v0/labs?show_all=true`. CML's plain `GET /api/v0/labs`
returns only labs owned by the logged-in user, so a lab created by another
account (including another administrator) would be missing. With `show_all`,
an administrator account receives every lab on the controller. A non-admin
account still receives only its own labs; that limit is enforced by CML.

```bash
# Confirm credentials with one lightweight GET, and write nothing
python -m cml_converter.src.fetch_from_cml \
    --host cml.example.com --probe

# One lab, by title or by id (repeat --lab to select several)
python -m cml_converter.src.fetch_from_cml \
    --host cml.example.com --lab "CSR 1000v Feature Tests" \
    --out cml_export.json
```

The JSON is `{"labs":[{"lab_title","nodes","links"}, ...]}`. `convert` keeps
**one lab in one area**. The area name is the lab title; `/`, `\`, and `'`
become `_`, and spaces stay. Tags and label heuristics that split a single
YAML export (for example `site1`) are not applied to a fetched lab. An
External Connector is still placed in `<lab>-ext` and drawn as a waypoint.
A device label that Network Sketcher would treat as the same name is
prefixed with its area. That includes the same spelling in two labs and
names that differ only by case inside one lab (`External` and `external`).
If the prefixed names still match once case is ignored, `_2`, `_3`, … is
appended. Links follow the rename because they cite node ids, and embedded
configs are read under the new label. A YAML file that is not this multi-lab
JSON keeps the previous area rules, and the same case-insensitive rename
applies there too.

## What is read from each node

- **Interfaces** come from `interface` stanzas at the top level of the config.
  An `interface` line nested deeper, such as inside `router isis`,
  `router ospf` or `sdwan`, is part of that block and does not replace the
  interface's addresses. A config indented as a whole (cloud-init style) and a
  flattened dump with every line at column 0 are both read.
- **Placement** follows the CML canvas. Nodes whose heights differ by more than
  20 px are on different rows, even on a wide, shallow canvas.
- **Attribute-D** carries a short routing summary, for example
  `Routing: IS-IS 1 L2 | BGP 64512 (ipv4, vpnv4) | SRv6 LOC1 2001:db8:1::/48 | VRF BLUE`.
  It lists IS-IS, OSPF / OSPFv3, EIGRP, BGP (AS and address families), SRv6
  locators, LDP and VRFs; when none are found it shows the start of the
  routing blocks instead. Up to 120 characters are kept.
- **Model / OS** come from `node_definition`. A custom definition named after
  its release (for example `iosxrd-cp-<version>`, the XRd Control Plane) is
  matched by the name before the version.
- **Not drawn**: MTU, VRF route distinguishers and route targets,
  route-policies and link conditioning have no place in the underlay. The
  overlay script names a VRF's RD and route targets in its cloud's Attribute.

## Output files

| File | Description |
|------|-------------|
| `ns_commands.txt` | Network Sketcher CLI commands (Phase 1–6) |
| `ns_model.json` | Intermediate topology model for debugging |
| `stencil_mapping.csv` | Device stencil-type mapping with confidence scores |
| `parse_report.md` | Per-device running-config coverage statistics, skipped ports, and nodes whose config was never extracted in CML |
| `<stem>_cml_overlay.txt` | `--mode overlay` / `both`: the integrated overlay script (Phase 1–6) |
| `ns_model_overlay.json` | `--mode overlay` / `both`: the overlay model for debugging |
| `overlay_report.md` | `--mode overlay` / `both`: overlay devices and their areas, handoffs (`confirmed` / `inferred`), links not drawn, day-0 nodes |

## Device color conventions

The generated `rename attribute_bulk` command writes a coloured cell into the **Default** column of the Network Sketcher Attribute sheet — `\"['DEVICE',[R,G,B]]\"` (WayPoints keep their token: `\"['WayPoint',[R,G,B]]\"`) — so every device is colour-coded by role in the Device Table. The palette and its meaning are **shared across every converter in this repo**:

| Colour | RGB | Meaning |
|--------|-----|---------|
| 🟩 Light green | `[235, 241, 222]` | **Observed network gear** — a real router / L3 switch / switch / firewall / WLC / AP present in the source data |
| 🟥 Light red | `[255, 204, 204]` | **Server-role endpoint** — server / controller / OT asset / internet service |
| 🟨 Light yellow | `[255, 255, 204]` | **Client endpoint** — PC / workstation / phone |
| 🟦 Light blue | `[220, 230, 242]` | **Observed WayPoint** — a WayPoint backed by a real source record. Also the fixed colour of the **Stencil Type** attribute column |
| ⬜ Light gray | `[200, 200, 200]` | **Inferred / not observed** — devices synthesised by the converter to complete a plausible topology (none currently — see below) |

The two WayPoint colours separate **observed** WayPoints (blue, backed by a real node in the lab) from **inferred** WayPoints (gray, invented by the converter with no real node behind them).

Two further fixed cell colours appear in every device row (set by the shared `ns_command_builder`, not role-based): the **Model** column is pink `[255, 183, 219]` and the **OS** column is light blue `[200, 230, 255]`.

**In cml_converter:** a node is a Waypoint only when its Attribute **Model** contains `External Connector` (the CML `external_connector` bridge to the host network). It renders **light blue**. Every other node is a normal device, including a router or switch whose name contains wan, internet, or cloud. Network gear (Router / L3Switch / Switch / Firewall / WLC / AP) is **green**, servers are **red**, and PC / Phone endpoints are **yellow**.

**In the overlay script:** the VPN / VRF clouds, the VRF-GWs, the NETs and the SD-WAN Edges / L3VPN PEs are **light purple** `[221, 204, 255]` (overlay), as in `nd_converter` and `sdwan_converter`. A `LAN:<edge>` dummy switch is **light gray**. CEs, LAN switches and hosts keep their role colour.

## Running the output in Network Sketcher

`ns_commands.txt` is a plain-text script (one command per line; `#` lines are
phase comments) already ordered Phase 1→6. Install
[Network Sketcher](https://github.com/cisco-open/network-sketcher), create an
empty master, run the command lines in order against it, and export the diagram.
See the
[top-level README "Running the output in Network Sketcher"](../README.md#running-the-output-in-network-sketcher)
for step-by-step instructions.

## Running-config files

Place one text file per device under a directory and pass it with `--configs`.
File stems must **exactly** match the CML node `label` (case-sensitive).

```
running_configs/
├── spine1.txt
├── leaf1.txt
└── border1.txt
```

## Supported CML YAML formats

- CML UI export (`Lab → Export → YAML`) — `lab: {nodes: [...], links: [...]}`
- CML REST API dump (`GET /api/v0/labs/{id}/topology`) — flat `nodes`/`links`
- Single-file combined topology dumps with top-level `topology:` key
- Multi-lab JSON from `fetch_from_cml` (`labs: [{lab_title, nodes, links}]`) — one lab, one area

## Directory structure

```
cml_converter/
├── README.md            (this file)
├── requirements.txt
├── .gitignore
└── src/                 (convert.py, fetch_from_cml.py, overlay_*.py)
```

## Changelog

### Overlay mode

- **`--mode {underlay,overlay,both}`**: `overlay` writes one integrated
  overlay script (VXLAN EVPN, SD-WAN and L3VPN found in the saved configs,
  with the handoffs between them) plus `ns_model_overlay.json` and
  `overlay_report.md`. The default `underlay` output is unchanged.
  `--overlay` narrows the types drawn. No overlay found exits with code 3.
- **Classic IOS `ip vrf forwarding <name>`**: the interface's VRF and the IP
  address after it were lost, because the line was taken for the start of a
  top-level `ip vrf` stanza. Both are now read (underlay and overlay).
- **Overlay on a LAG**: a VRF port on `Bundle-Ether` / `Port-channel` was left
  out of the overlay (with the CE behind it), because the CML link is on a
  member port. The member link and the port-channel are now drawn.
- **Overlay and underlay in one overlay script**: labs with no overlay are
  drawn as in the underlay, and an overlay lab's other devices are drawn as in
  the underlay and linked to the overlay devices by their underlay links.

### Untagged (native) VLAN notation

- **Native VLANs are written with `_UNTAG_`** (Network Sketcher RULE 9.5): a
  trunk's native VLAN (`switchport trunk native vlan N`, or VLAN 1 by default
  when the trunk carries it) is `VlanN_UNTAG_`, and a sub-interface with
  `encapsulation dot1Q N native` is bound to `VlanN_UNTAG_`. A port facing
  another port's `_UNTAG_` VLAN with the same tagged name (an access port, a
  sub-interface) becomes `_UNTAG_` too, so existing links keep their broadcast
  domain. The overlay copies these names with the underlay. **Requires
  Network Sketcher ver 3.1.2v or later** (the first version that reads
  `_UNTAG_`; an older one leaves native VLANs unconnected).
- **Fixed: `encapsulation dot1Q N`** (upper-case `Q`) was not read, so the
  sub-interface lost its VLAN binding.
- **Fixed: a sub-interface VLAN binding is written `'Vlan<id>'`**, quoted. The
  lower-case `vlan<id>` never joined the switch's `Vlan<id>` segment, and the
  unquoted form is rejected by the Network Sketcher MCP command validator.

### API fetch

- **CML REST fetch**: `fetch_from_cml.py` downloads lab topologies (read-only).
  Password via `--password` or `CML_PASSWORD`. `--probe` checks access and
  writes no file. The list call is `GET /api/v0/labs?show_all=true`, so an
  administrator account also includes labs owned by other users. Converting
  that JSON places each lab in its own area. Device names that match once
  case is ignored, such as `External` and `external`, are renamed so each
  device, its links, and its config stay distinct.
- **Port names**: the Catalyst SD-WAN Validator / vEdge form `ge0/0` becomes
  `GigabitEthernet 0/0`. An unknown type token is still split at the first
  digit (`<type> <number>`), because one port name without a space fails the
  whole `add l1_link_bulk`.
- **Waypoint area placement**: NS refuses an `add area_location` row that puts
  two waypoint-only (`*_wp_`) areas side by side, and that one rejection took
  the whole Phase 1 batch with it, leaving no areas for any later phase. A
  waypoint area now waits for the next ordinary area, or moves to its own row.
- **Unlinked ports**: NS only creates a physical port when an L1 link uses it.
  An IP, VRF, L2 segment, port-channel member, or sub-interface on a port
  with no link in the CML lab is no longer emitted (it stopped the script
  with "L3 interface not found"). Each one is listed under "Skipped ports"
  in `parse_report.md`.

### Ver 0.5.1b (2026-09-03)

- **Waypoints**: only a node whose Attribute Model contains `External Connector`
  is a Waypoint (light blue, `*_wp_` area). Every other node is a normal
  device, including routers and switches whose labels contain wan, internet,
  or cloud.


- **IOS-XR running-config L2/L3**: `config_parser.py` now recognises
  `interface Bundle-Ether<n>`, `ipv4 address`, `bundle id <n> mode <mode>`,
  `MgmtEth…`, and the bare per-interface / top-level `vrf <name>` form.
  `detect_os_family()` classifies IOS-XR **before** the IOS-XE
  `interface gigabitethernet0/0` substring (XR's `GigabitEthernet0/0/0/0`
  would otherwise match that prefix). `normalise_port_name()` keeps the LAG
  as `Bundle-Ether N` (Network Sketcher accepts that token; rewriting it to
  `Port-channel N` used to leave `add ip_address_bulk` targeting a port
  `add portchannel_bulk` never created). `MgmtEth0/RP0/CPU0/0` becomes
  `MgmtEth 0/0/0/0` so the engine's port-name sort key does not warn on
  `RP0`/`CPU0`.

- **Bug fix**: `topology_mapper.py`'s `assign_areas_and_rows()` passed a
  `default_color=` keyword argument to `NSDevice(...)` (intended to render an
  observed `external_connector` WayPoint in light blue instead of the
  inferred-WayPoint gray — see "Device color conventions" above) that the
  local `NSDevice` dataclass did not declare as a field. This raised
  `TypeError: __init__() got an unexpected keyword argument 'default_color'`
  for **every** CML lab, so `convert.py` could not complete a single
  conversion. Root cause: unlike the other converters in this repo,
  `cml_converter` defines `NSDevice` inline in `topology_mapper.py` instead of
  a separate `ns_model.py`; when the `default_color` override field (present
  in `template_converter/src/ns_model.py` and
  `3rd_party/netbox_converter/src/ns_model.py`) was reused here, the field
  declaration itself was never copied over. Fixed by adding the missing
  `default_color: Optional[Tuple[int, int, int]] = None` field to `NSDevice`
  and by making `ns_command_builder.cmd_rename_attribute_bulk()` actually
  consume it (a per-device colour override wins over the role-based colour),
  matching the pattern already used by `template_converter` /
  `netbox_converter`. Verified with a synthetic lab containing an
  `external_connector` node: `convert.py` now completes without exceptions
  and the generated `rename attribute_bulk` command colours the connector's
  `Default` cell `[220, 230, 242]` (light blue) instead of falling back to
  the generic WayPoint gray — confirmed live in Network Sketcher via the
  `network-sketcher` MCP (`row_colors` in the exported Device Table shows
  `rgb(220,230,242)` on the `inet_bridge` row).

- **Bug fix**: `stencil_mapper.py`'s keyword-heuristic fallback used to embed the
  matched keyword in single quotes when building the Model string (e.g.
  `Spine Switch (inferred from 'spine')`). Because the shared `ns_command_builder.py`
  escaping convention (`\'`) is not correctly round-tripped by the live Network
  Sketcher engine's `rename attribute_bulk` cell parser, this produced an
  `invalid syntax` warning at runtime and left the Model cell empty for every
  keyword-matched device. Fixed by dropping the quote characters instead of
  escaping them — the keyword is now embedded as plain text (e.g.
  `Spine Switch (inferred from spine)`). No change to `ns_command_builder.py`
  (shared across converters) or to the meaning of the Model string itself.

## Cisco Technologies

This tool bridges two Cisco technologies:

- **Cisco Modeling Labs (CML)** — network simulation platform for creating
  virtual network topologies
- **Network Sketcher** — open-source Cisco tool for designing and documenting
  network topologies using an AI-native CLI

## Author

Yusuke Ogawa - Architect, Cisco | CCIE#17583

## License

This tool is part of the **Network Sketcher Cisco Extension** project, licensed
under the [Apache License 2.0](../LICENSE). See the [NOTICE](../NOTICE) file for
copyright and third-party attributions.
