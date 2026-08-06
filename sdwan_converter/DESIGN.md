# `sdwan_converter` — Detailed Design

> **Purpose of this document**: the full design rationale, worked examples, Network-Sketcher-engine
> behaviour research and the input JSON schema for `sdwan_converter`, kept out of `README.md` so that
> file can stay a short, task-oriented quick reference. `README.md` cross-references this document by
> section number (e.g. "see DESIGN.md section 3.7") wherever it summarises something explained here in
> full. Every worked example and count in this document was produced by actually running the converter
> against the bundled `Input_data/sample_sdwan_export.json` (12 devices: 1 vManage, 2 vSmart, 1 vBond,
> 8 Edges across 5 sites — `br1`/`br2` one Edge each, `br3`/`dc1`/`dc2` two Edges each — on transport
> colours `biz-internet` and `mpls`, plus two colour-unresolved VPN0 ports on `br3`'s two Edges) unless
> stated otherwise.

---

## 0. Executive Summary

`sdwan_converter` turns a Cisco Catalyst SD-WAN (vManage-managed) fabric export into two independent
[Network Sketcher](https://github.com/cisco-open/network-sketcher) command scripts: a physical
**underlay** (every device's real ports, the inferred/observed WAN transport circuits above the Edges,
and their real Service VPN LAN side below them) and a logical **overlay** (the Service VPN/VRF
membership abstraction, one waypoint cloud per VPN). Both are generated **offline** from a single
combined JSON that `fetch_from_vmanage.py` pulls over vManage's read-only REST API. See `README.md`
for the day-to-day usage contract (quick start, modes, output files, mapping-table summaries,
configuration keys); this document is the "why" behind every one of those rules, plus the appendices
(input JSON schema, NS-engine behaviour research, historical changes) that do not belong in a
quick-reference README.

| Area | Summary |
|---|---|
| Underlay scope | VPN0 physical transport circuits (observed via LLDP/CDP, else inferred by TLOC colour) + the Service VPN LAN side + the controllers' control-plane path — §3 |
| Overlay scope | Service VPN (VRF) membership only, one waypoint cloud per VPN — §4 |
| Shared conventions | Interface-to-diagram classification, area naming, the `Dummy 0` SVI pattern, Loopback handling — §2 |
| Biggest source of divergence from a 1:1 model | SD-WAN has no concept of "cabling" between two Edges; every cross-device relationship in both diagrams is either inferred (transport colour), observed best-effort (LLDP/CDP, unconfirmed field names), or a deliberate abstraction (one cloud per VPN/colour) — §5 |
| Data source constraints that shaped the design | No route table (`ip/routetable` is always empty), no confirmed LLDP/CDP schema, VPN names are template metadata not running-config — §5, §6 |

---

## 1. Architecture

### 1.1 Module responsibility table

| Module | Responsibility |
|---|---|
| `fetch_from_vmanage.py` | Read-only REST client. Logs in, pulls device/interface/wan-interface/LLDP/CDP/BFD state per device plus one fabric-wide Service-VPN-name pass, writes the combined JSON (§6) |
| `sdwan_export_reader.py` | Loads the combined JSON (or a directory of per-endpoint `*.json` dumps) into an in-memory index (`idx.devices`, `idx.interfaces`, `idx.wan_interfaces`, `idx.control_connections`, ...) |
| `sdwan_topology.py` | Pure data-classification helpers shared by both mappers: device personality, VPN0/Service-VPN/Loopback/Tunnel interface filters, transport-colour resolution, control-connection joins, BFD mesh-shape classification, site-area-name resolution |
| `sdwan_physical_mapper.py` | Builds the **underlay** `NSModel`: transport clouds, controllers' control-plane path, VPN0 VRF, calls `sdwan_lan_side` for the LAN side |
| `sdwan_logical_mapper.py` | Builds the **overlay** `NSModel`: Service VPN waypoint clouds, `Vpn <id>` membership ports, calls `sdwan_lan_side` for the LAN side |
| `sdwan_lan_side.py` | The **one** shared planner for the Service VPN LAN side (per-Edge `Dummy_<n>` switch naming/assignment, physical VRF ports, Loopbacks) that both mappers call, so the two diagrams can never disagree on it |
| `sdwan_stencil_mapper.py` | vManage `personality`/`device-model` → NS stencil (`Server`/`Router`), with a confidence score recorded in `sdwan_inventory.csv` |
| `ns_model.py` | Repo-shared `NSModel`/`NSDevice`/`NSL1Link`/... dataclasses, port-name normalisation, area-layout helpers (`build_area_layout`) — used by every converter in this repo, not sdwan-specific |
| `ns_command_builder.py` | Repo-shared: `NSModel` → Phase 1–6 NS CLI command text — used by every converter in this repo |
| `convert.py` | CLI entry point: argument parsing, mode dispatch, writes the `*.txt` scripts, `ns_model_*.json`, `sdwan_inventory.csv` and the `*_report.md` files |

### 1.2 Why one shared `NSModel`/`ns_command_builder` but two mappers

The underlay and overlay are too different to share a mapper (different device sets, different link
directions, different VRF rules — see the "Why two diagrams" comparison table in `README.md`), but they
draw the **identical** LAN side (same `Dummy_<n>` switches, same Edge assignment, same VRF names). That
shared slice is factored into `sdwan_lan_side.py`, called by both mappers, so the two diagrams cannot
drift apart on it by construction rather than by discipline. Everything downstream of `NSModel`
(`ns_command_builder.py`, the area-layout algorithm) is the same code every converter in this repository
uses — this converter does not fork the shared rendering pipeline anywhere.

---

## 2. Processing Pipeline & Shared Conventions

### 2.1 Command phases

Both command files are ordered Phase 1→6 and apply cleanly top-to-bottom:

| Phase | Verb | Underlay | Overlay |
|---|---|---|---|
| 1 | `add area_location` / `add device_location` | ✅ | ✅ |
| 2 | `add l1_link_bulk` | ✅ | ✅ |
| 2.5 | `rename port_info_bulk` | ✅ | ✅ |
| 3 | `add virtual_port_bulk` (SVIs + Loopbacks) | ✅ | ✅ |
| 3 | `add l2_segment_bulk` | ✅ | ✅ |
| 4 | `add ip_address_bulk` | ✅ | ✅ |
| 4 | `rename l3_instance` (VRF) | ✅ | ✅ |
| 6 | `rename attribute_bulk` | ✅ | ✅ |

`add virtual_port_bulk` must precede `add l2_segment_bulk` (a port must exist before it can be bound to
a segment), which is why both share phase number 3 but are emitted as two separate blocks in that
order. The underlay did not emit `virtual_port_bulk` or `rename l3_instance` at all before the
Service-VPN-LAN-side and `Dummy 0` SVI features were added (§7); both are now first-class parts of its
output.

### 2.2 VPN0, Tunnel`<N>` and the Service VPNs: what maps where

**VPN 0 is a routing context (the Transport VPN), not a single interface.** Every VPN0-tagged
interface on an Edge — physical AND logical alike — is just a member of that global routing table;
there is no one interface that "is" VPN0. Everything with a *non-reserved* `vpn-id` (10, 11, 12, ...)
is instead a member of a **Service VPN**: an independent customer VRF. This classification (confirmed
against Cisco's SD-WAN documentation and a live vManage) is what both mappers filter on before they
draw anything:

| Interface | `vpn-id` | Real / virtual | This converter draws it in... |
|---|---|---|---|
| `GigabitEthernetN` / `TenGigabitEthernetN` / ... | `0` | Real physical port | **Underlay only** (`sdwan_physical_mapper.py`) — the WAN transport circuit: L1 link to its transport cloud + IP + the `Transport_vpn0` VRF on the Edge port (§3.6) |
| `GigabitEthernetN` / ... (incl. dot1q sub-interfaces) | Service VPN (10, 11, ...) | Real physical port | **Both diagrams** — overlay: an L1 link UP to its VPN's cloud (+ L2 segment on the cloud side only); underlay: an L1 link DOWN to the Edge's LAN switch. IP + VRF on the Edge port either way |
| `LoopbackN` | Service VPN | Real logical interface | **Both diagrams**, as an UNCONNECTED virtual port (IP + VRF only, no L1/L2) — §2.5 |
| `TunnelN` | `0` | **Real** logical interface, auto-created by IOS-XE (cEdge) when a physical VPN0 interface has `tunnel-interface`/`encapsulation` enabled — it genuinely appears in `show interface` and vManage's own interface list | **Neither diagram.** It is bound to exactly one physical interface via `tunnel source <physical-if>` and shares that interface's colour and IPv4 address, so drawing it would only duplicate the underlay's physical port |
| `NVI0`, `Sdwan-system-intf`, `vmanage_system`, `LoopbackN` in VPN0 | `0` | Pseudo/system interfaces (control-plane plumbing, no transport role) | **excluded from both** diagrams |
| anything in VPN `512` / `65528`–`65530` | reserved | out-of-band management / internal | **excluded from both** diagrams, unconditionally |

### 2.3 Area naming: `site-name` first

Both diagrams name each NS area after the site's **`site-name`** as reported by
`/dataservice/device` (`site-id 3` → `br1`, controllers → `controllers`), far more readable than a bare
number. Resolution rules (`sdwan_topology.build_site_area_map`, shared by both mappers so they can
never disagree):

- the most frequently reported `site-name` across that site-id's devices wins (alphabetically first on
  a tie);
- characters outside `[A-Za-z0-9_.-]` are folded to `_`;
- a site-id whose devices report **no** `site-name` falls back to `site<id>` (the behaviour before this
  feature, so an older export is unaffected);
- if two **different** site-ids resolve to the same name, every one of them is disambiguated to
  `<name>_site<id>` — otherwise two genuinely separate sites would be fused into one NS area;
- a device with no `site-id` at all goes to the `default` area.

No extra API call is needed: `site-name` is already part of each raw `/dataservice/device` record.
(The `/dataservice/site*` paths returned HTTP 404 on the vManage this was validated against, and
`/dataservice/template/policy/list/site` both disagreed with `site-name` and omitted some sites, so
neither is used.) The bundled sample's five site areas are `dc1`, `dc2`, `br1`, `br2`, `br3` plus
`controllers` — every device reports a `site-name`, so none of them falls back to `site<id>`.

### 2.4 Both modes: every WayPoint cloud carries a `Dummy 0` SVI

In **both** diagrams, each WayPoint cloud gets one extra port called exactly `Dummy 0`, modelled as an
**SVI** (`add virtual_port_bulk`) and **self-bound** to that cloud's own L2 segment
(`add l2_segment_bulk`) — Network Sketcher's RULE 15 "mandatory SVI-to-L2-segment binding" pattern:

| | Underlay | Overlay |
|---|---|---|
| Cloud | one per transport colour/group (`biz-internet`, `mpls`, `unknown_1`, `unknown_2`) | one per Service VPN (`Corporate`, `PCI`, `Guest`) |
| Segment it binds to | the transport colour/label (§3.4) | the VPN label (§4.4) |

It is bound to the **same segment name** the cloud's `Dummy 1`…`Dummy N` link ports already carry, so
the SVI and those ports can never drift apart. The port has **no L1 link** and **no IP address**: it
exists only to give the segment an L3/SVI anchor on the cloud side. Like everything else on these
clouds it is **synthesised** — there is no `Dummy 0` interface, and no cloud, anywhere in the vManage
data. In the underlay the missing IP is also what lets a transport cloud span several access subnets
(§3.2).

`Dummy 0` cannot collide with the cloud's L1 link ports, which are numbered from `Dummy 1` upwards in
both mappers. Both mappers count the SVIs (`cloud_svi_ports`) and list them under `SYNTHETIC` in their
report — measured on the bundled sample: 4 in the underlay (one per transport group) and 3 in the
overlay (one per Service VPN).

### 2.5 Loopbacks (both modes)

A Service VPN Loopback becomes an **unconnected** virtual port — `add virtual_port_bulk`, its IP, and
its VRF, with **no** L1 link and **no** L2 segment — so it appears on the L3 diagram only. The underlay
draws them identically, from the same shared plan (`sdwan_lan_side.py`). This follows Network
Sketcher's own rule set, where RULE 15 ("Mandatory SVI-to-L2 segment binding") names Loopback as an
explicit exception, and matches every other converter in this repo: none of them has ever made a
Loopback an L1-link endpoint. The bundled sample carries 2–3 Loopbacks per Edge (`Loopback 10`/`11` on
the two-VPN datacenter Edges, `Loopback 10`/`11`/`12` on the three-VPN branch Edges) — 20 in total,
drawn identically by both diagrams.

---

## 3. Underlay detailed design

### 3.1 Underlay L1 links: observed vs. inferred

The underlay draws each SD-WAN Edge's VPN0 **physical** interface as an L1 link in one of two ways,
preferring the first that resolves:

1. **OBSERVED** — if vManage's LLDP/CDP neighbor endpoints (`/dataservice/device/lldp/neighbors`,
   `/dataservice/device/cdp/neighbors`) report a neighbor for that port that resolves to another
   SD-WAN-managed device, a real device-to-device link is drawn with both real port names (LLDP
   preferred over CDP when both report the same port, since LLDP is universal and CDP is
   IOS-XE/cEdge-only).
2. **INFERRED** (fallback) — for every VPN0 physical interface not resolved by step 1, interfaces
   sharing a **transport colour** are grouped into one gray cloud named after that colour (a star per
   group) — see §3.2.

> [!IMPORTANT]
> This has been validated end-to-end **only in inference-fallback mode**. The one live vManage this
> converter has been tested against (a CML-simulated lab) does not expose LLDP/CDP neighbor data over
> its REST API at all, so every underlay diagram produced so far — including the bundled sample —
> uses the INFERRED path exclusively (`observed_links: 0` in every report generated to date). The
> OBSERVED path is implemented against a best-effort, unconfirmed field-name convention (§5.4) and
> should be checked against a real physical vManage's actual response the first time you rely on it
> there. Set `enable_observed_l1_links` to `false` in the configuration to force the inference-only
> behaviour unconditionally.

### 3.2 One transport cloud per colour, and the `unknown_N` exception

Any remaining VPN0 physical interface with a real IPv4 address is grouped by its **transport colour
alone** — the IPv4 subnet is **not** part of the key, since a TLOC colour is the transport's identity
in SD-WAN. Every member Edge links to that colour's cloud with its real port + IP (a star, never a
mesh), so one cloud can carry several access subnets: in the bundled sample, `biz-internet` carries
both `172.16.1.0/24` and `172.16.100.0/24`, and `mpls` carries both `172.16.2.0/24` and
`172.16.200.0/24`. A colour with only one member Edge still gets its own single-member cloud —
representing that Edge's dedicated WAN access circuit — rather than being dropped or merged away.

**The `unknown_N` exception.** A VPN0 interface whose colour could **not** be resolved against
`control/waninterface` (by IP address match) does *not* fall back to a shared `unknown` cloud with
every other unresolved interface. `unknown_color_label` (config key, default `"unknown"`) is a
placeholder meaning "could not be identified" — it is **not** a real, shared TLOC colour, so there is
no evidence that two colour-unresolved interfaces actually ride the same WAN circuit. Merging them on
that basis alone would draw a false adjacency exactly as wrong as merging two unrelated real colours.
Each colour-unresolved interface therefore gets its **own** single-member cloud instead, built in a
two-pass process:

1. **Pass 1** collects, for every VPN0 physical interface, its resolved colour (or `None` if
   unresolved). The `(device, port)` pairs with `None` are sorted alphabetically and assigned a
   sequence number.
2. **Pass 2** builds the final `edge_ports` grouping key: a resolved colour uses the colour string
   itself; an unresolved one uses `f"{unknown_color_label}_{i}"` (`unknown_1`, `unknown_2`, ...) so the
   existing "one cloud per key" grouping logic naturally produces one cloud per unresolved interface
   without a separate code path.

Each such cloud's `colour=` attribute still reports the plain `unknown_color_label` value (`unknown`)
— that IS its real, if unidentified, colour — while only the cloud's **name**/identity carries the
disambiguating sequence number, e.g. `colour=unknown | members=1 (inferred) | networks=172.16.100.0/24`.
Counted in both reports as `unresolved_color` (interfaces) and `unresolved_color_segments` (clouds
created — always equal to `unresolved_color` by construction, since each such interface is guaranteed
its own cloud).

**Measured on the bundled sample**: `br3-edge1`'s `GigabitEthernet 6` (`172.16.100.0/24`) and
`br3-edge2`'s `GigabitEthernet 6` (`172.16.200.0/24`) both fail to resolve a colour and land in
`unknown_1` and `unknown_2` respectively — two entirely separate single-member clouds, never merged
together, even though both happen to use the same placeholder label. `transport_segments: 4`
(`biz-internet`, `mpls`, `unknown_1`, `unknown_2`), `unresolved_color: 2`, `unresolved_color_segments: 2`,
`single_member_segments: 2` (the two `unknown_N` clouds — the two resolved colours both have all 8
Edges as members).

### 3.3 Underlay mesh-shape annotation

When the export includes OBSERVED live BFD tunnel-session state (`bfd_sessions`, OPTIONAL — §6), each
inferred transport segment's cloud label gains a parenthetical suffix — `Full Mesh`, `Hub-and-Spoke`,
or `Partial Mesh` — computed by `sdwan_topology.classify_mesh_shape`. It answers "*how densely does the
SD-WAN fabric actually mesh across the Edges attached to this circuit?*" from those Edges'
`state: up` BFD sessions (excluding same-site Edge pairs, which were observed to not form a
colour-specific BFD adjacency to each other in this converter's live validation environment):

- **Full Mesh** — every distinct-site pair of member Edges has an observed `up` session on that colour.
- **Hub-and-Spoke** — a small subset of "hub" Edges each connect to every other member Edge, while
  every remaining "spoke" Edge connects ONLY to the hub(s) — never directly to another spoke.
- **Partial Mesh** — anything else (some sessions observed, but neither of the above patterns).
- **No suffix at all** — no BFD session data for that segment's Edges, **or** the segment has fewer
  than 3 member Edges (a "mesh shape" is meaningless for a single pair). This mirrors the
  LLDP/CDP-based L1-link inference's own "no data → no annotation" fallback — which is why the two
  single-member `unknown_N` clouds never get a suffix.

The classification is scoped to the **members of that one segment**, not to every Edge using the
colour fabric-wide — one transport colour routinely spans several independent circuits (both
`biz-internet` and `mpls` in the bundled sample carry two access subnets each), and classifying
colour-wide would mislabel them.

This is an ANNOTATION on the label only — the star drawing (Edge port → segment cloud) never changes,
and the actual Edge-to-Edge mesh is still never drawn as links. vManage's centralized-policy APIs
(`/dataservice/template/policy/definition/mesh` / `hubandspoke` / ...) were evaluated as an alternative
data source but rejected: in this converter's live validation environment every vSmart policy was
inactive (`isPolicyActivated: false`) and the one existing "control" policy targeted a Service VPN, not
VPN0 — so policy definitions do not reliably reflect the actual live mesh shape, while observed BFD
session state does.

**Measured on the bundled sample**: both `biz-internet` and `mpls` (8 member Edges each) classify as
`Full Mesh` (`mesh_annotated: 2`). The two `unknown_N` clouds have only 1 member each, so neither is
annotated.

### 3.4 Underlay: why the L2 segment is cloud-side only

Each inferred transport cloud carries exactly **one** `add l2_segment_bulk` entry per `Dummy N` port,
named after the **transport colour/group** (`biz-internet`, `mpls`, `unknown_1`, `unknown_2`) — and
**no** L2 segment is placed on the Edge's `GigabitEthernet N` port, or on any device other than the
cloud itself. (The cloud's own `Dummy 0` SVI is self-bound to that same segment — §2.4.)

The segment is what makes the Underlay's L2 and L3 diagrams meaningful at all. Without it, each
Edge↔cloud link is its own two-node broadcast domain, so the L3 diagram has no subnet to draw and comes
out **empty**. With it, every Edge attached to one colour lands in a single L3 broadcast domain — for
the bundled sample, one domain per transport cloud: all 8 Edges on `Gi 1` for `biz-internet` (carrying
both `172.16.1.0/24` and `172.16.100.0/24`), all 8 on `Gi 2` for `mpls` (`172.16.2.0/24` and
`172.16.200.0/24`), and one Edge each on `unknown_1`/`unknown_2`. `l2_segments: 19` in total (8+8 cloud
link ports across the two resolved colours + 2 unresolved + the 4 `Dummy 0` self-bindings − see §2.4 −
plus the 5 controller-segment entries of §3.7).

Keeping it on the cloud side only is the same **asymmetric-L2** pattern the overlay uses (§4.4), for
the same hard reason: in Network Sketcher **any port that carries an L2 segment is treated as an L2
switchport and is removed from the L3 interface table**, so segmenting the Edge's physical WAN port
would make its `add ip_address_bulk` fail with `L3 interface not found` and silently strip its IP
address. The Edge's VPN0 interface really *is* a routed L3 port, so it stays segment-free; the cloud's
internal `Dummy N` ports are what need grouping.

The segment name is the colour, and so is the cloud's own name: the merge is exactly **one cloud per
colour/group**, so the segment name is unambiguous by construction — even for a cloud spanning several
access subnets, which is one broadcast domain by design.

### 3.5 Underlay: the Service VPN LAN side

The Underlay is **not VPN-0-only**. Above each Edge it draws the VPN0 transport circuits; **below** it,
it draws the very same LAN side the Overlay does — for `br1-edge1` in the bundled sample:

```
wan_wp_               [ biz-internet ]                        [ mpls ]
                          │                                        │
br1              'GigabitEthernet 1'                    'GigabitEthernet 2'   ← REAL, IP +
                    172.16.1.5/24                          172.16.2.5/24         Transport_vpn0
                          └────────────────┬───────────────────────┘
                                    [ br1-edge1 ]                             ← tier row 1
                                           │            'Loopback 10/11/12'   ← REAL, IP + VRF,
                          ┌────────────────┼─────────────┐   (unconnected)       no link
                'GigabitEthernet 3'      'Gi 4'        'Gi 5'                 ← REAL, IP + VRF
                   10.10.3.2/24       10.11.3.2/24  10.12.3.2/24
                          └────────────────┼─────────────┘
                                    [  Dummy_1  ]                             ← SYNTHETIC, tier row 4
                                   (split by VRF, no IP)
```

(`Loopback 10`/`11`/`12` carry `10.10.0.5/32`/`10.11.0.5/32`/`10.12.0.5/32` respectively, in
`Corporate_vpn10`/`PCI_vpn11`/`Guest_vpn12`.)

Everything below the Edge — the `Dummy_<n>` switch and its numbering, the LAN links, the IPs, the
Loopbacks and every VRF label — is produced by `sdwan_lan_side`, the **one** module both mappers call,
so the two diagrams place the identical switch under the identical Edge with the identical VRF name
(`Dummy_1` = `br1-edge1`, `Dummy_2` = `br2-edge1`, `Dummy_3` = `br3-edge1`, `Dummy_4` = `br3-edge2`,
`Dummy_5` = `dc1-edge1`, `Dummy_6` = `dc1-edge2`, `Dummy_7` = `dc2-edge1`, `Dummy_8` = `dc2-edge2` for
the bundled sample) and cannot drift apart. The switches are light gray in **both** diagrams, for the
same reason: they are synthesised placeholders with no vManage counterpart.

What stays exclusive to the Overlay is the Service VPN **cloud abstraction**: the synthetic `Vpn <id>`
membership ports and the per-VPN waypoint clouds are never drawn in the Underlay. Conversely the
transport clouds are never drawn in the Overlay. The LAN side is the deliberate overlap between the
two.

Layout follows RULE 0 (top-to-bottom hierarchy) and RULE 0.5 (L1 crossing avoidance): transport clouds
in `wan_wp_` on their own row at the top, the Edges on their site area's first row, and the LAN switches
on a second row below them — `add device_location "['br1',[['br1-edge1'],['Dummy_1']]]"`, the same
two-row form the Overlay already emits.

The three configuration keys that shape this side — `excluded_service_vpns`, `vrf_name_format` and
`lan_dummy_name_format` — therefore apply to **both** modes, not to the overlay alone.
`transport_vpn_name` (§3.6) is underlay-only.

**Measured on the bundled sample**: `lan_dummy: 8`, `lan_links: 20`, `service_vpn_loopbacks: 20`,
`lan_vrf_renames: 60`.

### 3.6 Underlay: the VPN 0 transport VRF (`Transport_vpn0`)

Every Edge's VPN0 physical transport interface (`GigabitEthernet 1` / `GigabitEthernet 2`, plus the
colour-unresolved `GigabitEthernet 6` on the two `br3` Edges) carries **VPN 0 itself as its L3
instance**:

```
rename l3_instance 'br1-edge1' 'GigabitEthernet 1' 'Transport_vpn0'
rename l3_instance 'br1-edge1' 'GigabitEthernet 2' 'Transport_vpn0'
```

so the Underlay's L3 interface table separates the SD-WAN Transport VPN from the Service VPNs instead
of pooling it into an unnamed default. Nothing else about those ports changes: same IP, same L1 link to
the transport cloud.

The name is not hardcoded. It goes through the **same** `vrf_label_for_vpn` / `vrf_name_format` path as
`Corporate_vpn10`, called with vpn-id `0` and the `transport_vpn_name` configuration value (default
`Transport`), so `{name}_vpn{id}` yields `Transport_vpn0`. Blank `transport_vpn_name` to fall back to a
bare `vpn0`; change `vrf_name_format` and both the transport VRF and the Service VPN VRFs follow it
together.

> [!NOTE]
> The VPN 0 name is deliberately **not** read from the export's own `vpn_names`, unlike every Service
> VPN name. vManage's VPN 0 template is usually called something like `VPN0`, which would render as the
> redundant `VPN0_vpn0`; the configuration value gives a stable, readable label instead.

**Edge side only — the transport clouds get no VRF.** No `rename l3_instance` targets a transport
cloud: its `Dummy N` link ports and its `Dummy 0` SVI carry the transport-colour L2 segment and nothing
else. This mirrors the Overlay exactly, where the Edges and the LAN switches get the VRF but the
`Corporate` / `PCI` / `Guest` waypoint clouds never do — and it is also what keeps those cloud ports
valid L2 switchports for the L2/L3 renderer. **Measured on the bundled sample**:
`transport_vrf_renames: 18` (2 per Edge on the two resolved colours, plus 1 each for `br3-edge1`'s and
`br3-edge2`'s colour-unresolved `Gi 6` — 8×2 + 2 = 18).

### 3.7 Underlay: the controllers' control-plane path

The controllers are no longer unconnected inventory nodes. Their control traffic **is** visible in
vManage and it **does** ride the VPN 0 transport this underlay already draws — but they cannot simply
be cabled to a transport cloud, because they are not on any Edge transport subnet. They are hung off
two synthesised hops instead. The `controllers` area is the **top** row of `add area_location`, so the
path reads downwards into the transport clouds exactly like an Edge's own uplink; the bundled sample
(1 vManage, 2 vSmart, 1 vBond) produces exactly this:

```
        [Controller01] [Controller02]     [Manager01]      [Validator01]     ← area controllers (TOP row)
            vSmart          vSmart           vManage            vBond
                                          Ethernet 1        GigabitEthernet 0/0    real VPN 0 ports;
                                        172.16.0.1/24        172.16.0.201/24        IP where resolved
                    │              │           │                  │
                 Dummy 1        Dummy 2     Dummy 3            Dummy 4
                     [                    Dummy_l2                     ]     ← SYNTHETIC, Switch
                                        Dummy 0                                 all 4 ports in the
                                           │                                    'dummy_l2' segment
                                        Dummy 1
                     [                    Dummy_l3                     ]     ← SYNTHETIC, Router
                                        Dummy 0                                (no L2 segment, no IP)
                                           │
                                        Dummy 9
                     [                mpls (Full Mesh)                 ]    ← area wan_wp_
```

**What the data says.** `/dataservice/device/control/[synced/]connections` returns, per Edge, one row
per control connection that Edge holds up to a controller: the controller's `peer-type` and
`system-ip`, the **Edge's own** `local-color`, and the controller's `private-ip`/`public-ip`. That is
enough to say *which* transport circuit carries the control plane. It is **not** enough to say how far
away the controller is: controllers sit on their own subnet, disjoint from every Edge transport subnet,
and `/dataservice/device/ip/routetable` answers with an empty `data` array, so the number of routed hops
is unobtainable. Linking a controller straight to a transport cloud would therefore assert an adjacency
the data contradicts.

**What is drawn instead.** Two synthesised light-gray devices, stacked **below** the controllers inside
their own area (the area itself sits above the transport clouds, so the whole construct points
downwards):

- **`Dummy_l2`** (`Switch` stencil) stands in for the segment the controllers really do share. Every
  controller links to it **on its own real VPN 0 port**, and it carries one L2 segment named
  `dummy_l2` on **all** of its own ports — the link down to `Dummy_l3` included — and on no controller
  port, the same cloud-side-only asymmetry the transport clouds use (§3.4).
- **`Dummy_l3`** (`Router` stencil) stands in for the unknown routed hop. It is linked DOWN to
  **exactly one** transport cloud — the most likely carrier of the control plane, see below — and UP
  to `Dummy_l2`. It carries no L2 segment, which is what makes it read as a router rather than a
  switchport.

Ports on both devices are pseudo ports numbered from `Dummy 0` upwards. This deliberately differs from
the transport clouds, which reserve `Dummy 0` for their RULE 15 SVI and start their link ports at
`Dummy 1`; there is no SVI here.

**Which cloud, confirmed by address rather than trusted by name.** The row's `local-color` is resolved
through that Edge's `control/waninterface` entries to its real transport address, and that address is
matched against the Edge's already-resolved VPN 0 ports; the colour of the port it lands on is the
cloud the row votes for. Since the clouds merge by colour, that selection is now a **colour**, not a
colour+subnet — but going through the addresses is still worth doing: a row naming a colour this Edge
has no VPN 0 port for votes for nothing and is counted as unresolved instead of being taken at face
value.

**Exactly ONE uplink, picked by weight of evidence.** The control plane really does leave the fabric
over a single routed path, so linking `Dummy_l3` to every cloud it was observed over would turn it into
a hub touching all of them. Instead the observed groups are ranked and only the strongest is drawn, in
this deterministic order:

| Rank criterion | Why |
|---|---|
| 1. Observed control-connection **rows** | the most direct measure of how much control traffic was seen over that circuit |
| 2. Number of **controllers** reached | a circuit that reaches all controllers beats one that reaches one |
| 3. Number of **member Edges** on the cloud | a cloud more Edges sit on is the likelier path off the fabric |
| 4. Cloud **name** | tie-break only, so the output is stable |

**Measured on the bundled sample**: `mpls (Full Mesh)` wins with 24 rows / 3 controllers / 8 member
Edges, against 16 rows / 2 controllers / 8 member Edges for `biz-internet (Full Mesh)`
(`control_uplinks: 1`, `control_conn_rows: 40`, `edges_with_control_data: 8`). The runner-up is **not**
lost: the chosen cloud, its score, and every observed-but-undrawn cloud with its score go into the
underlay report's caveats.

**Each controller's own VPN 0 port carries its reported IP address, where one resolves.** A controller
is not a routing node — it is a `Server`-role appliance — so **no VRF** is written on this port; the
Transport VPN concept is an Edge-only construct, and the controller stays in Network Sketcher's default
L3 instance while the *Edges'* VPN 0 ports stay in `Transport_vpn0`. VPN 512 stays excluded, and the two
SYNTHETIC hops get no IP at all — they stand in for structure that was never measured. **Measured on the
bundled sample**: 2 of the 4 controllers (`Manager01`, `Validator01`) report a usable VPN0 IPv4 address
and get it drawn (`control_ip_assignments: 2`); a controller whose export data resolves no usable
address for this pass is drawn on the shared `Dummy_l2` segment without one — the report's count is the
ground truth to check per export, rather than assuming every controller always resolves one. Two
consequences worth knowing when a controller *does* carry L3 content: the `controllers` area then has
L3 content and no longer sorts last (§5.2), and the NS L3 renderer tints its device label (§5.3).

**Per-controller nuance is annotated, not drawn.** Because `Dummy_l3` draws one uplink, the fact that
(say) a controller is reachable over `mpls` only while others use both colours would otherwise be lost.
It is preserved as an annotation — on each controller (`control plane: mpls`), on `Dummy_l3` itself, and
in full in the underlay report — following the same precedent as the BFD mesh-shape suffix (§3.3),
which likewise annotates a real but non-adjacent relationship rather than drawing it. **Measured on the
bundled sample**: `Controller01` over `biz-internet, mpls`; `Controller02` over `biz-internet, mpls`;
`Manager01` over `mpls`; `Validator01` history only (torn down).

**The vBond is a special case.** Its control connections are torn down once onboarding completes, so
`control/connections` returns zero rows for it and it appears only in `control/connectionshistory`,
with `state: tear_down` and an unusable `system-ip` of `0.0.0.0` (joinable to the device by its
transport address alone). It is therefore **drawn on the shared segment** — which is a fact about its
subnet — but contributes **no** uplink and is annotated `control plane: history only`. A torn-down
session is not current connectivity. `controllers_history_only: 1` on the bundled sample (the vBond).

**Fallback and gate.** If the export carries no `control_connections` data, or none of it can be joined
to a known controller and an inferred transport segment, the controllers are drawn exactly as before
this feature existed (unconnected inventory nodes) and the report gets a caveat saying so. No colour and
no address is ever synthesised to bridge the gap. Setting `enable_controller_control_plane_links` to
`false` forces that same behaviour unconditionally: the `controllers` area falls back to its
single-row form and no `Dummy_l3` / `Dummy_l2` / `dummy_l2` line is emitted at all — and with no L1
link, no controller IP either. The area is still promoted to the top row (that is pure layout, see
§3.8).

> [!NOTE]
> **Viptela-OS vs IOS-XE interface schema.** `/dataservice/device/interface` answers in two different
> shapes depending on the queried platform's OS, and a controller is always Viptela-OS even in a fabric
> whose Edges are all IOS-XE cEdges:
>
> | | IOS-XE (cEdge) | Viptela-OS (controllers, vEdge) |
> |---|---|---|
> | `ifname` | `GigabitEthernet1` | `eth1`, `ge0/0` |
> | Prefix length | separate `ipv4-subnet-mask` field | inside `ip-address` (`172.16.0.1/24`), **no** mask field |
>
> Both forms are accepted on the controller path (`sdwan_topology.controller_vpn0_interfaces` /
> `interface_cidr`), and `normalise_port_name` renders `eth1` → `Ethernet 1` and `ge0/0` →
> `GigabitEthernet 0/0`. A controller's **VPN 512** port (vManage's `eth0`) is excluded by the VPN 0
> filter, as it should be: *that* is the genuine out-of-band management network — the control traffic
> itself is not out-of-band.
>
> A vSmart in this bundled sample also exposes several **additional** VPN0-tagged physical NICs
> (`eth0`, `eth2`, `eth3`) that carry no IPv4 address at all (vManage represents "no address" on some
> of these rows as the literal string `"-"` rather than an empty field or `0.0.0.0`). This is why
> `controller_vpn0_interfaces()`'s "does it report a usable address" filter, not simple interface-name
> ordering, is what should ultimately decide which port is drawn — worth double-checking against your
> own vManage if a controller with multiple VPN0 NICs comes out with an unexpected port name.

### 3.8 Underlay: the controllers' area is the top row

`build_area_layout` puts every non-waypoint area on **one** row below the waypoint row, which left the
controllers beside the sites and their control-plane path climbing back **up** over the transport
clouds. The underlay lifts them out of that row into a row of their own at the very top, giving a
three-row layout:

```
add area_location "[['controllers'],['wan_wp_'],['br1','br2','br3','dc1','dc2']]"
```

and the order **inside** the area is reversed to match — controllers on top, then `Dummy_l2`, then
`Dummy_l3` nearest the clouds it links down to:

```
add device_location "['controllers',[['Controller01','Controller02','Manager01','Validator01'],['Dummy_l2'],['Dummy_l3']]]"
```

Everything then points downwards, the way each Edge's own transport uplink already does (NS RULE 0).
This is a post-pass (`_promote_controller_area_to_top`) applied right after the L3-less reordering of
§5.2, exactly so the shared `ns_model.build_area_layout` helper — used by every converter in this repo —
stays untouched. The rule is driven by device personality, not by a hardcoded area name, and an area
that also holds an Edge (a controller sharing a `site-id` with one) is left where the layout helper put
it, so lifting the controllers can never drag a site out of the site row.

The **column** each row starts in, however, is entirely out of this converter's control — Network
Sketcher's layout engine decides it from each row's rendered width. See §5.1 for the full research into
that behaviour, including the specific measurements that show why a leading spacer or padding trick
cannot influence it.

---

## 4. Overlay detailed design

### 4.1 Overlay: WAN side up, LAN side down

Each Edge is drawn with its **VPN membership above** it and its **real LAN ports below** it, so a site
reads the way it actually works — for `br1-edge1` in the bundled sample:

```
overlay_wp_        [ Corporate ]      [ PCI ]      [ Guest ]     ← one cloud per VPN
                        │                │             │           (L2 segment HERE only)
br1                 'Vpn 10'        'Vpn 11'      'Vpn 12'       ← SYNTHETIC, VRF, no IP
                        └────────────────┼─────────────┘
                                  [ br1-edge1 ]                  ← tier row 0
                        ┌────────────────┼─────────────┐
              'GigabitEthernet 3'  'Gi 4'         'Gi 5'         ← REAL, real IP + VRF
                10.10.3.2/24     10.11.3.2/24  10.12.3.2/24
                        └────────────────┼─────────────┘
                                  [  Dummy_1  ]                  ← SYNTHETIC, tier row 4
                                 (split by VRF, no IP)
```

> [!IMPORTANT]
> **The `Vpn <id>` ports and the `Dummy_<n>` LAN switches do not exist in the vManage data.** They are
> drawing devices this converter invents:
>
> - A `Vpn <id>` port is **not an interface**. It represents the Edge's *membership* of that Service
>   VPN, which is why it carries a VRF but no IP address. The real overlay data path is the SD-WAN
>   IPsec/OMP fabric riding VPN 0 — drawn in the **underlay** diagram, not here.
> - A `Dummy_<n>` switch is **not a real device**. It is one synthetic aggregation point per Edge, so
>   the Edge's real LAN ports terminate somewhere below it (separated by VRF) instead of floating
>   unconnected. Whatever LAN switching actually exists behind those ports is invisible to vManage.
>
> Both are listed under `SYNTHETIC` in `sdwan_overlay_report.md`.

Splitting the two directions is what makes the real interface data survive: the physical port keeps its
own IP and subnet on the L3 diagram (it is a genuine routed interface in a VRF), while the "which VPNs
is this Edge in?" question is answered by the links going up to the clouds, without either use fighting
the other for the same port.

### 4.2 Overlay: VRF names

The L3 instance (VRF) written by `rename l3_instance` is **`<VPN name>_vpn<id>`** — `Corporate_vpn10`,
`PCI_vpn11`, `Guest_vpn12` (format configurable via `vrf_name_format`). A VPN with no configured name
uses plain **`vpn<id>`** (`vpn12`) rather than the cloud's `VPN <id>` fallback label, which would
produce a redundant `VPN 12_vpn12`. The numeric id is always present so the VRF stays unambiguous in
the L3 interface table even if two VPNs share a name.

The same VRF is written onto every port that belongs to that VPN: the Edge's `Vpn <id>` port, its
physical LAN ports, the facing LAN-switch ports, and its Loopbacks. The LAN switch is therefore split
by VRF internally rather than reading as one flat LAN. **Measured on the bundled sample**: the three
branch Edges (`br1-edge1`, `br2-edge1`, `br3-edge1`, `br3-edge2`) carry all three VRFs
(`Corporate_vpn10`, `PCI_vpn11`, `Guest_vpn12`); the four datacenter Edges (`dc1-edge1`, `dc1-edge2`,
`dc2-edge1`, `dc2-edge2`) carry only `Corporate_vpn10` and `PCI_vpn11` — the `Guest` VPN is
branch-only, as in a real fabric.

### 4.3 Overlay: one vpn-id = one cloud

Each Service VPN gets exactly ONE waypoint cloud **fabric-wide**, which every member Edge's interfaces
in that VPN attach to. In reality each site normally has its own subnet inside a given VPN, so this is
a deliberate **abstraction**: the L2 diagram shows one broadcast domain per VPN (which is what "everyone
in this VRF can reach everyone else" looks like at a glance), while the L3 diagram still separates the
real per-site subnets correctly, because every port keeps its own reported IP/mask. **Measured on the
bundled sample**: `service_vpn_waypoint: 3` (`Corporate`, `PCI`, `Guest`).

### 4.4 Overlay: why the L2 segment is cloud-side only

Each WAN-side overlay link is deliberately **asymmetric**. Exactly one L2 segment — the VPN label,
nothing else — is placed on the **cloud's `Dummy N` port**. **No** L2 segment is placed anywhere else:
not on the Edge's `Vpn <id>` port, not on its physical Service VPN ports, and not on any LAN-switch
port. The LAN-side links carry no L2 segment at all.

This is not a stylistic choice. In Network Sketcher, **any port that carries an L2 segment is treated
as an L2 switchport and is removed from the L3 interface table.** Putting the VPN segment on any of
those ports therefore makes it unaddressable: `add ip_address_bulk` reports
`L3 interface not found: <device>:<port>` and `rename l3_instance` reports
`No matching entry found for hostname ... and portname ...`, so the port silently ends up with neither
its IP nor its VRF.

The asymmetric arrangement is also the physically honest one. An Edge's Service VPN interface really
*is* a routed L3 interface inside a VRF, so it belongs in the L3 interface table with its IP and
`l3_instance`. The segment exists purely to group the cloud's internal ports into **one broadcast
domain per VPN** in the L2 diagram, which only requires the cloud side to carry it.

The segment name is the **VPN name** (`Corporate`), a deliberate departure from the `Vlan<id>`
convention the other converters in this repo use: there is no VLAN in an SD-WAN Service VPN, and
Network Sketcher renders a non-`Vlan`-prefixed segment name on a WayPoint's `Dummy N` port correctly
(verified). **Measured on the bundled sample**: `l2_segments: 20` in the overlay (one per Edge×VPN
membership link, e.g. `br1-edge1` contributes 3).

### 4.5 Overlay: Service VPN names

VPN names come from vManage's **feature templates** (`/dataservice/template/feature` →
`templateDefinition.vpn-id` / `.name`), scanned once fabric-wide by `fetch_from_vmanage.py` and stored
in the export's `vpn_names` block (§6). They are therefore a **template setting, not device
running-config**: an untemplated device, or a fabric managed entirely through Configuration Groups, may
resolve no names at all. In that case each cloud falls back to the `vpn_name_fallback_format`
configuration value (default `VPN {id}`, e.g. `VPN 10`), the VRF falls back to `vpn<id>` (§4.2), and the
count is reported in `sdwan_overlay_report.md` (`unresolved_vpn_name: 0` on the bundled sample — all
three VPNs resolve a name).

A fabric-wide scan is used deliberately in preference to walking each device's own attached
device-template chain. That chain was **verified to break** on a live vManage: some devices report a
device-template name that does not exist under `/dataservice/template/device` at all, and
`/dataservice/template/device/config/attached/{id}` returns empty for every device in a
Config-Group-managed fabric. When two templates pin the same `vpn-id` to different names, the winner
is: a user-defined template beats a `factoryDefault` one, then the higher `attachedMastersCount`, then
the alphabetically first `templateName`.

### 4.6 Overlay L3 IP addresses

Each Service VPN interface has its reported IPv4 address/mask drawn on the overlay's L3 diagram
(`add ip_address_bulk`) and its VPN's VRF written as that port's L3 instance (`rename l3_instance`), so
the L3 diagram is separated by VRF exactly as the real fabric is. The mask reported by vManage is used
as-is; a host `/32` is a fallback used only when that mask is missing or unparseable — not the case
anywhere in the bundled sample (`ip_assignments: 40`, every one carrying its export-reported mask).

Only the Edge's **own real interfaces** (physical VRF ports and Loopbacks) ever get an IP. The
synthetic `Vpn <id>` ports, the LAN-switch ports and the Service VPN waypoint clouds carry a VRF at
most — none of them is a real Layer-3 endpoint.

---

## 5. Known limitations & risk register

### 5.1 Underlay area column positioning is engine-controlled, not converter-controlled

`add area_location` emits a **grid of rows**, and that is the whole of its authority: it fixes which
row an area sits on and the order of the areas within that row. The **column** each row starts in is
computed by Network Sketcher and cannot be influenced by the emitted script. On a build with two
transport colours, this can mean the `controllers` row starts one column to the *left* of `wan_wp_`:

```
All Areas Position (illustrative — two transport colours)

controllers |         |     |     |     |
            | wan_wp_ |     |     |     |
            | br1     |     | br2 |     | dc1
```

The mechanism is in `recalculate_folder_sizes()`, the pass the engine runs at the end of **every**
layout or device mutation. It gives each row a `total_width` — the sum of the rendered widths of that
row's areas, with empty cells explicitly *excluded* from the sum — finds the single widest row, strips
that row's leading empty cell so it starts at column 1, and forces a leading empty cell onto every other
row so they start at column 2. Because empty cells contribute nothing to `total_width`, no arrangement
of `''` cells can move a row. Measured directly against the NS engine using purpose-built test masters,
these four emitted forms all produced the **identical** `POSITION_FOLDER`:

| emitted form | result |
| --- | --- |
| `[['','controllers'],['wan_wp_'],[…]]` (leading spacer) | `controllers` col 1 |
| `[['controllers','','','',''],['wan_wp_','','','',''],[…]]` (pad every row to the widest row's element count) | `controllers` col 1 |
| `[['controllers'],['','wan_wp_'],[…]]` (spacer on the waypoint row) | `controllers` col 1 |
| `[['','','controllers',''],['','wan_wp_','']],[…]]` (leading + trailing on every row) | `controllers` col 1 |

Padding every row to the widest row's element count is the form that looks most promising, since
`make_position_folder_tuple` only injects side padding into rows that are not the longest
(`needs_side_empty = (row_idx != max_row_idx)`). It does not work: that function calls
`remove_trailing_empty_cells()` on each row *before* it measures them, so the padding is gone by the
time `max_row_idx` is computed, and `recalculate_folder_sizes()` then overrides the result on width
anyway. There is also no other lever — `add area_location` is the only CLI verb that touches the area
grid at all; nothing sets an area's column, width, or offset.

What actually decides the outcome is how many devices each area holds. Three runs of the **same**
emitted command against test masters built with one, two and three transport colours (purpose-built to
isolate this one variable, independent of the bundled sample's own device count):

| transport colours | `wan_wp_` width | `controllers` width | widest row | resulting grid |
| --- | --- | --- | --- | --- |
| 1 | 3.7601 | 5.5392 | `controllers` | `controllers` col 1, `wan_wp_` col 2 |
| 2 | 5.5073 | 5.5392 | `controllers` | `controllers` col 1, `wan_wp_` col 2 |
| 3 | 7.5073 | 5.5392 | `wan_wp_` | `wan_wp_` col 1, `controllers` col 2 |

The three-colour fabric flips the arrangement purely because the waypoint row finally out-grows the
three side-by-side controllers — and in the two-colour case the two rows are within 0.03 inch of each
other, so the flip is decided by a 0.6% width difference. Earlier versions of this converter emitted a
leading `''` spacer on the controllers row and re-applied the line as a final
`# Phase: 6 area_location (re-apply)` command; both have been removed, because the measurements above
show the spacer changes nothing and the re-apply had nothing left to restore.

None of this changes what the diagram says. The controllers still occupy their own row above the
transport clouds, with their control-plane path running downwards, which is the relationship the
drawing is read for; only the horizontal offset of that row varies with the fabric.

One reading note: `show area_location` strips empty cells when it prints, so it is not a reliable way
to inspect the grid. Read `POSITION_FOLDER` or the Device Table's *All Areas Position* tab instead.

### 5.2 Areas with no L3 interface sort last

Within a row of `add area_location`, any area that holds **no IP-bearing (L3) interface** is emitted
**last**, with the remaining areas keeping their normal order — `[['br1','br2','dc1','dc2','site9']]`
rather than `[['br1','br2','site9','dc1','dc2']]`.

This is a **workaround for an upstream Network Sketcher engine bug**, not a layout preference. The
all-areas L3 renderer's `calculate_area_offset()` positions each area at the sum of the widths of the
areas *preceding* it in its row, but its width lookup only contains areas that actually carry L3
content — so an L3-less area sitting anywhere but last aborts the whole L3 export with
`KeyError: '<area>'`. A row that is L3-less **end to end** is left alone: the renderer never looks it
up, which is why the `wan_wp_` waypoint row is safe (and why the `controllers` row was safe before the
controllers' VPN 0 addresses were assigned; today that area carries L3 content like any other, via
§3.7's `control_ip_assignments`).

The rule is general (any L3-less area sorts last, no hardcoded area name). The **overlay** does not
need it: an Edge is only drawn there if it has at least one Service VPN interface *with a usable IPv4
address*, so every overlay site area necessarily carries L3 content.

### 5.3 Known Network Sketcher behaviour: the L3 diagram tints device labels

The colours in `README.md`'s "Device color conventions" section are what the **L1/L2 diagrams and the
Device Table** show. In the **L3 diagram** the same devices come out slightly purple, and this is the
NS L3 renderer's own behaviour, **not** something this converter emits.

Measured on the generated underlay SVGs, NS adds a fixed `+(15, 10, 25)` RGB offset to the name cell of
every device that carries L3 content:

| Device | Requested (Default cell) | L1/L2 + Device Table | L3 diagram |
|---|---|---|---|
| SD-WAN Edge | `[235, 241, 222]` | `rgb(235,241,222)` | `rgb(250,251,247)` |
| `Dummy_<n>` LAN switch | `[200, 200, 200]` | `rgb(200,200,200)` | `rgb(215,210,225)` |
| Controller (vManage / vSmart / vBond) with an assigned IP | `[255, 204, 204]` | `rgb(255,204,204)` | `rgb(255,214,229)` (red clamped at 255) |
| `Dummy_l3` / `Dummy_l2` (no L3 content) | `[200, 200, 200]` | `rgb(200,200,200)` | `rgb(200,200,200)` |

The VRF blocks themselves are a separate fixed colour (`rgb(230,224,236)`) that no converter controls
either.

Because the two synthetic control-plane hops carry no IP, they stay true gray in the L3 diagram. A
controller that does carry one (§3.7) picks up the same shift there. Cancelling the offset would mean
requesting a pre-compensated colour (roughly `185, 190, 175` to land on gray) — which would make the
L1/L2 diagrams and the Device Table greenish-gray instead, so it is deliberately **not** done. Fixing it
belongs upstream in [network-sketcher](https://github.com/cisco-open/network-sketcher).

### 5.4 OBSERVED L1 link field names are unconfirmed against a real vManage

The scope-limit callout at the top of `README.md` exists because of this specific gap: the one live
vManage this converter has been validated against returns HTTP 404 for both
`/dataservice/device/lldp/neighbors` and `/dataservice/device/cdp/neighbors`, so the parser's field-name
guesses for those two endpoints have never been exercised against real data — only the INFERRED
fallback path (§3.1) has. The parser tries several plausible key names per field (see the illustrative
example in §6.3), based on the general shape other vManage endpoints use, but this is a best-effort
convention, not a confirmed schema. Verify it against your own vManage's actual response before relying
on the OBSERVED path there; `enable_observed_l1_links: false` disables the attempt entirely and forces
pure colour-based inference.

By contrast, the BFD mesh-shape endpoint's field names (`color`, `system-ip`, `site-id`, `state`) *have*
been verified against a live vManage (§3.3) — so no field-name caveat applies to the mesh-shape
annotation, only to the two real caveats noted there (same-site Edge pairs excluded from the mesh check,
and the classification reflecting observed live state rather than policy intent).

### 5.5 WayPoint left-to-right ordering is an estimate, not a guaranteed pixel-optimal layout

Both diagrams order same-row WayPoint clouds (e.g. multiple transport-colour / Service VPN clouds) to
minimise a placement-time **estimated** total wire length, weighted by L1-link count to each linked
site device's column position. The real on-canvas pixel length is only known once Network Sketcher
actually renders the diagram, so this ordering is a good-faith heuristic applied before that rendering
happens, not a guarantee that the final SVG has zero avoidable crossings.

### 5.6 `personality` inference from `device-model` is best-effort

`device-model` naming is not fully standardised across vManage releases/platforms (`vedge-cloud`,
`vedge-C8000V`, `c8000v`, `vedge-1000`, ...). This fallback only triggers when the `personality` field
itself is absent from the export — the common case has `personality` present and uses it directly.
When the fallback does trigger, it is a lower-confidence keyword match (0.85 vs. 1.00), flagged as such
in `sdwan_inventory.csv`.

### 5.7 Concepts that can NOT be fully represented — detailed rationale

`README.md`'s "⚠ Concepts that can NOT be fully represented" table is the reader-facing summary; the
rationale behind each row is the corresponding section above (§3.1 for cabling, §3.3 for the tunnel
mesh / mesh-shape annotation, §4.3 for the Service-VPN-cloud abstraction, §4.1/§3.5 for the synthetic
WAN/LAN scaffolding, §3.6 for `Transport_vpn0`, §4.5 for Service VPN names, §3.7 for the controllers'
control-plane path, §5.6 for `device-model` inference). Two points are broad enough to restate here
rather than tie to one section:

- **No OMP routes, service chaining, or control policy.** What each Service VPN actually advertises,
  and any policy that restricts VPN-to-VPN or site-to-site reachability inside it, is not modelled: the
  overlay shows VRF *membership*, not the resulting reachability matrix.
- **This is a faithful, deliberately scoped-down approximation, not a 1:1 model**, by the same logic
  that governs every other converter in this repo dealing with a policy/overlay fabric (`aci_converter`
  being the closest sibling) — Network Sketcher is fundamentally a VLAN/port diagram tool, and SD-WAN is
  a colour-keyed IPsec+BFD tunnel mesh over an arbitrary transport underlay. Always validate against
  vManage before relying on either diagram.

---

## 6. Input JSON schema (appendix)

### 6.1 Combined JSON format (also the offline input)

```json
{
  "_meta": {"source": "Cisco SD-WAN Manager (vManage)", "host": "..."},
  "devices":        [ <device>, ... ],
  "interfaces":     { "<system-ip>": [ <interface>, ... ] },
  "wan_interfaces": { "<system-ip>": [ <waninterface>, ... ] },
  "lldp_neighbors": { "<system-ip>": [ <lldp-neighbor>, ... ] },
  "cdp_neighbors":  { "<system-ip>": [ <cdp-neighbor>, ... ] },
  "bfd_sessions":   { "<system-ip>": [ <bfd-session>, ... ] },
  "vpn_names":      { "10": "Corporate", "11": "PCI", "12": "Guest" }
}
```

Each `<device>` is a raw `/dataservice/device` record (at minimum `host-name`, `system-ip`, `site-id`,
`device-model`, `personality`, `uuid`, `reachability`), normally also including `site-name` — the
human-readable site label both diagrams use as the NS area name (§2.3). Each `<interface>` is a raw
`/dataservice/device/interface` record (`ifname`, `vpn-id`, `ip-address`, `ipv4-subnet-mask`,
`if-admin-status`). An `af-type` field (`ipv4`/`ipv6`) is present on some records and absent on others
(a real vManage appears to only emit it for certain interface/device kinds — see the bundled sample);
nothing in this converter filters on it either way, and (per §3.7's note) a "no address" IPv4 row can
be reported as the literal string `"-"` rather than an empty field, which any code reading `ip-address`
directly (rather than through `sdwan_topology.interface_ip`) should account for. Service VPN membership
comes solely from each interface's `vpn-id` — `/dataservice/device/vpn` was observed to always return an
empty `"data": []` array and is therefore not used. Each `<waninterface>` is a raw
`/dataservice/device/control/waninterface` record (`color`, `private-ip`, `public-ip`). Each
`<bfd-session>` is a raw `/dataservice/device/bfd/sessions` record (`color`, `system-ip` of the REMOTE
peer, `site-id` of the remote peer, `state`) — §3.3; unlike the neighbor records below, this endpoint's
field names WERE verified against a live vManage.

`vpn_names` maps a Service VPN id to its configured name. Keys are always **strings** on read, even
though vManage's template API reports the id as an integer while its interface API reports the same id
as a string.

`lldp_neighbors` / `cdp_neighbors` / `bfd_sessions` / `vpn_names` are all **optional** — the bundled
sample happens to carry `bfd_sessions` and `vpn_names` (real data) but empty/absent `lldp_neighbors` and
`cdp_neighbors` (see §5.4), and a hand-built or trimmed export can omit any of them; the converter falls
back gracefully (no mesh-shape suffix, no Service VPN names / `VPN <id>` labels, no OBSERVED L1 links,
respectively). `lldp_neighbors` / `cdp_neighbors` are also absent on any export from a platform whose
vManage does not expose the endpoint. Each `<lldp-neighbor>` / `<cdp-neighbor>` is whatever raw record
`/dataservice/device/lldp/neighbors` / `/dataservice/device/cdp/neighbors` returns; **the exact field
names are UNVERIFIED against a real physical vManage** (§5.4), so the parser tries several plausible key
names per field. For illustration only — this is a synthetic shape, not a captured real response — a
populated entry might look like:

```json
{
  "lldp_neighbors": {
    "10.0.1.1": [
      {
        "local-interface": "GigabitEthernet1",
        "chassis-id": "00aa.bbcc.ddee",
        "system-name": "isp-pe-router-1",
        "port-id": "TenGigabitEthernet0/0/1",
        "management-ip": "203.0.113.5"
      }
    ]
  }
}
```

`convert.py` also accepts a **directory** of per-endpoint `*.json` dumps (filenames hint the collection,
e.g. `devices.json`, `interfaces.json`).

### 6.2 Fetch sequence and field provenance (`fetch_from_vmanage.py`)

Retrieval is **read-only** (GET only, plus the one unavoidable login POST) — it never modifies the
SD-WAN fabric. It authenticates with vManage's form-based login (`POST /j_security_check` with
`j_username`/`j_password`, session cookie), then fetches the CSRF token required on every subsequent
request (`GET /dataservice/client/token`, sent back as the `X-XSRF-TOKEN` header). Credentials come from
a CLI arg or the `VMANAGE_PASSWORD` env var (never hard-coded, never logged); vManage's typically
self-signed lab certificate is accepted by default (`--verify-tls` to enforce verification).

| Endpoint | Populates | Notes |
|---|---|---|
| `/dataservice/device` | `devices` | keyed by `system-ip` for every subsequent per-device call |
| `/dataservice/device/interface?deviceId=<system-ip>` | `interfaces` | |
| `/dataservice/device/control/waninterface?deviceId=<system-ip>` | `wan_interfaces` | TLOC colour resolution (§3.2) |
| `/dataservice/device/lldp/neighbors?deviceId=<system-ip>` | `lldp_neighbors` | best-effort, preferred OBSERVED source (§3.1, §5.4) |
| `/dataservice/device/cdp/neighbors?deviceId=<system-ip>` | `cdp_neighbors` | best-effort, secondary OBSERVED source |
| `/dataservice/device/bfd/sessions?deviceId=<system-ip>` | `bfd_sessions` | mesh-shape annotation (§3.3); field names verified |
| `/dataservice/device/control/[synced/]connections` and `/connectionshistory` | (control-plane path, not persisted verbatim) | controllers' uplink resolution (§3.7) |
| `/dataservice/template/feature` (fabric-wide, once) | `vpn_names` | Service VPN naming (§4.5) |

All five per-device endpoints require the **`system-ip`**, not the device `uuid`, in `deviceId`. Each
per-device query is independent and a failed one is **skipped, not fatal** — a partial fetch still
produces the best diagram the data allows. This matters most for the LLDP/CDP neighbor calls: they are
not guaranteed to exist on every vManage release/platform, so a 404/empty response from either is
treated exactly like any other optional endpoint here.

The Service VPN name pass is the one endpoint fetched **fabric-wide** rather than per device: every
feature template is listed, each `templateType` of `cisco_vpn` (IOS-XE/cEdge) or `vpn-vedge`
(Viptela-OS/vEdge) is fetched in full, and a `{vpn-id, name}` pair is accepted only when BOTH are
`vipType: "constant"` (a `variableName` means the template does not pin the value fabric-wide) — see
§4.5 for the conflict-resolution order and why a per-device template chain is deliberately not used
instead.

---

## 7. Changelog (historical behaviour evolution)

Kept for context on how the current design (§2–§4) was arrived at; every command file produced today
reflects the *end state* described above, not the intermediate steps below.

1. **Underlay gains an L2 segment, and areas with no L3 interface sort last.** The underlay was
   originally L1-only. It now also emits one L2 segment per transport cloud (cloud side only, named
   after the transport colour — §3.4) so the Underlay L2/L3 diagrams are populated instead of empty,
   and it sorts L3-less areas to the end of the area row to avoid the NS engine crash described in
   §5.2. The underlay command file was accordingly renamed from `<stem>_sdwan_underlay(L1Only).txt` to
   `<stem>_sdwan_underlay.txt`. Everything else the underlay emitted before was byte-for-byte
   unchanged, and the overlay was unaffected.
2. **A `Dummy 0` SVI on every WayPoint cloud, and gray overlay LAN switches.** Both modes gained one
   `Dummy 0` SVI per WayPoint cloud, self-bound to that cloud's existing L2 segment (§2.4) — the
   underlay's first `add virtual_port_bulk` phase. The overlay's synthetic `Dummy_<n>` LAN switches
   changed from light purple to light gray `[200, 200, 200]`, matching the shared palette's
   "synthesised placeholder" meaning; the Edges and the Service VPN clouds stayed light purple.
3. **The underlay draws the Service VPN LAN side, puts every Edge port in a VRF, and connects the
   controllers.** The single largest behaviour change: below each Edge, the underlay started drawing
   the same LAN side the overlay does (§3.5, via the new shared `sdwan_lan_side.py`); above each Edge,
   its VPN0 transport interfaces gained the `Transport_vpn0` VRF (§3.6); and in the `controllers` area,
   the controllers gained real connectivity through the synthetic `Dummy_l3`/`Dummy_l2` hops (§3.7). The
   underlay gained its first `rename l3_instance` phase. The overlay's command file was completely
   unaffected by this change (verified byte-for-byte identical).
4. **The controllers move to the top row, get their IP addresses, and hang off ONE transport cloud.**
   Follow-on refinement of change 3: `add area_location` became three rows with the controllers'
   internal order reversed (§3.8); each controller's own VPN 0 port started carrying its reported IP
   address where one resolves (§3.7), giving the `controllers` area L3 content and exempting it from
   the L3-less-area reordering of §5.2; and `Dummy_l3` was changed to link to exactly the single
   most-likely transport cloud (ranked per §3.7) instead of to every cloud it was ever observed over.
   The overlay was, again, completely unaffected.
5. **Colour-unresolved VPN0 interfaces stop being merged into one shared `unknown` cloud.** Originally,
   every interface whose TLOC colour could not be resolved fell back to one common `unknown_color_label`
   cloud, which could draw a false shared-circuit relationship between two Edges that merely both failed
   to resolve a colour (observed on real data: two different branch Edges' unrelated WAN circuits landed
   in the same cloud). Fixed by the two-pass `unknown_N` scheme in §3.2: each such interface now gets its
   own single-member cloud, and a new `unresolved_color_segments` counter tracks how many were created.
6. **The bundled sample was consolidated from two synthetic files into one sanitized real export.**
   `Input_data/sample_sdwan_export.json` (the original hand-built 7-device synthetic fabric) and
   `sample_sdwan_export_with_bfd.json` (a second file added purely to exercise the BFD mesh-shape
   feature) were replaced by a single `sample_sdwan_export.json`: a **sanitized real 12-device export**
   captured from a CML-simulated vManage lab (`tools/sanitize_vmanage_export.py` replaces device
   `uuid`/`board-serial`/geo-coordinates/timestamps and the `_meta.host` URL only — every IP address,
   VPN/VRF, BFD session and control-connection value other than its timestamps is untouched real fabric
   data). This is the sample every worked example and count in this document and in `README.md` is now
   measured against.
7. **Sanitization matches timestamps by shape, and fails loud on anything it did not normalize.**
   `tools/sanitize_vmanage_export.py` originally normalized an enumerated three-key timestamp list
   (`lastupdated`, `uptime-date`, `last-conn-time-date`), which left the export's real capture window
   readable from `downtime`, `downtime-date`, `createTimeStamp` and the time embedded in
   `vdevice-dataKey`. It now normalizes by value shape instead: a 13-digit epoch-ms value under a key
   whose name contains `time`/`date`/`stamp` becomes the fixed `1700000000000`, and an ISO-8601
   date-time **substring** in any string value becomes the equivalent fixed `2023-11-14T22:13:20+0000`,
   leaving the rest of a composite value (`vdevice-dataKey`'s IP, index and separators) intact.
   Requiring both the key name and the value shape keeps duration-style siblings such as `uptime`
   (`"0:01:18:50"`) and `timezone` (`"UTC +0000"`) untouched. The script no longer fails open either:
   after rewriting it re-scans its own output for foreign epochs, foreign ISO date-times,
   canonical-format UUIDs, non-`SAMPLE` `uuid`/serial values, real geo-coordinates and an unreplaced
   host URL, and on any hit it prints each offending JSON path to stderr, **writes no output file** and
   exits non-zero (`--allow-residual` downgrades this to a warning). Re-running it over an
   already-sanitized file is idempotent — values already in `SAMPLE-<HOST>-UUID` / `SAMPLE0001` form are
   preserved rather than renumbered, so cross-references cannot be shuffled. Applying the broadened
   rules to the committed sample changed 148 values across `createTimeStamp`, `downtime`,
   `downtime-date` and `vdevice-dataKey` only, and left both modes' command files byte-for-byte
   identical.
