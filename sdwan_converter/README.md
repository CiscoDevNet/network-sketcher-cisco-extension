# sdwan_converter (Beta) — Cisco Catalyst SD-WAN (vManage) to Network Sketcher Command Converter

> [!NOTE]
> **Beta — not a formal release.** Two things are still unproven. The converter
> has been validated end-to-end against **one** CML-simulated vManage lab only,
> so no other fabric size, release or platform has exercised it yet; and the
> OBSERVED (LLDP/CDP) path for the underlay's L1 links is implemented against a
> best-effort, **unconfirmed field-name convention** — that lab's vManage
> returns HTTP 404 for both neighbor endpoints, so every diagram produced so
> far took the INFERRED fallback (see "Underlay L1 links: observed vs.
> inferred" and the accuracy caveats below). Always validate the output against
> your own vManage before relying on it.

Convert a [Cisco Catalyst SD-WAN](https://www.cisco.com/site/us/en/products/networking/sd-wan/index.html)
(vManage-managed) fabric into ready-to-run
[Network Sketcher](https://github.com/cisco-open/network-sketcher) command
scripts. The model is pulled from vManage over its **read-only REST API**
(`fetch_from_vmanage.py`) — or from a previously downloaded export — and
converted **entirely offline** into two diagrams.

---

## Why two diagrams: underlay vs. overlay

**SD-WAN deliberately decouples the WAN transport that carries the fabric
(VPN 0) from the customer traffic the fabric carries (the Service VPNs / VRFs
— VPN 10, 11, 12, ...).** The transport side is about *cables and circuits*:
every Edge's VPN0 physical interfaces just need *some* IP reachability to
vManage/vBond over each transport. The Service VPN side is about *who can
talk to whom*: each VRF is an independent routing domain that OMP advertises
fabric-wide, completely indifferent to which physical circuit it rides. They
answer different questions and do **not** line up 1:1, so this tool produces
**two independent diagrams** (selectable with `--mode`) instead of forcing
them into one misleading picture:

| | **Underlay** (`--mode underlay`) | **Overlay** (`--mode overlay`) |
|---|---|---|
| Question it answers | *Which physical ports does each Edge actually have, and how do they reach the WAN transport(s) above and the LAN below?* | *Which Service VPNs (VRFs) does each Edge participate in, on which ports and subnets?* |
| Contents | vManage/vSmart/vBond + every Edge, with INFERRED shared-transport-circuit clouds **above** it and its own synthetic per-Edge LAN switch **below** it — the **physical** picture, both sides | Every Edge that has ≥1 Service VPN interface, with one waypoint cloud per Service VPN **above** it and one synthetic per-Edge LAN switch **below** it — the Service VPN **abstraction** |
| Layer | L1 (physical VPN0 interface up, physical Service VPN interface down) + L2 (the transport colour as a cloud-side segment; none on the LAN side) + L3 (every physical port's IP and VRF, plus the Service VPN Loopbacks) | L1 (synthetic `Vpn <id>` port up, physical Service VPN interface down) + L2 (the VPN as a cloud-side segment) + L3 (each port's IP and VRF) |
| The `Dummy_<n>` LAN switches | ✅ identical names, identical Edge→switch assignment (planned by the shared `sdwan_lan_side` helper) | ✅ identical names, identical Edge→switch assignment |
| Controllers (vManage / vSmart / vBond) | ✅ drawn **and connected**, in the TOP area row: hung **above** the one transport cloud their OBSERVED control connections most point at, through the two synthetic `Dummy_l3` / `Dummy_l2` hops (L1 + L2 only, no IP), with their own VPN 0 ports carrying their reported IP | ❌ not drawn (they hold no Service VPN interface) |
| VRFs | `Transport_vpn0` on the Edges' VPN0 transport ports + the Service VPN VRFs on the LAN side — every drawn Edge port has one. The controllers get **none** (Server-role appliances) | the Service VPN VRFs only |
| VPN 0 transport ports | ✅ drawn, with their IP, their L1 link to a transport cloud and the `Transport_vpn0` VRF | ❌ not drawn |
| Service VPN cloud abstraction (`Vpn <id>` ports + per-VPN waypoint clouds) | ❌ not drawn | ✅ drawn |
| Output | `*_sdwan_underlay.txt` | `*_sdwan_overlay.txt` |

**The short version of the split, now that the Underlay is no longer
VPN-0-only:** the **Underlay** is the *physical* diagram — every real port an
Edge has, WAN transport above and Service VPN LAN below — while the **Overlay**
is the *Service VPN abstraction* — one cloud per VRF that every member Edge
attaches to. They still share the exact same LAN side, deliberately, so the two
diagrams line up device for device.

> **What vManage's core device/interface APIs do NOT provide** — unlike
> NDFC/Catalyst Center (real observed cabling) and like ACI (config-only, no
> cabling), vManage's device/interface APIs return per-device operational
> STATE only (which VPN0 physical port carries which IP/colour), with no
> adjacency between two Edges' WAN ports built in. This converter also
> attempts vManage's separate LLDP/CDP neighbor endpoints as a **preferred,
> best-effort OBSERVED source** for the underlay's L1 links (see "Underlay
> L1 links: observed vs. inferred" below); when that data is present, it
> takes priority over inference. When it is absent (as with every export
> validated so far), the underlay's transport circuits fall back to being
> **inferred** (grouped by shared transport colour), exactly like
> `aci_converter` must infer ACI's leaf-spine mesh.

**Explicitly out of scope for BOTH diagrams (by design):** the real
IPsec+BFD tunnel-to-tunnel full/partial mesh between Edges (never drawn as
links), VPN0 **Tunnel** interfaces (drawn in neither diagram — see "VPN0 and
Tunnel`<N>`" below), and OMP-advertised routes / centralized control policy.
See "Concepts that can NOT be represented" below. Observed live BFD *session
state* (`/dataservice/device/bfd/sessions`) is the one partial exception:
when present, it is used **only** to classify and label each **underlay**
transport segment's fabric mesh shape (Full Mesh / Hub-and-Spoke / Partial
Mesh — see "Underlay mesh-shape annotation" below); it never adds
Edge-to-Edge links to either diagram.

---

## Overview

| Item | Detail |
|------|--------|
| **Input** | vManage model via the read-only REST API (`fetch_from_vmanage` → a combined JSON), **or** a previously saved combined JSON / directory of `*.json` |
| **Output** | Two NS command scripts (`*_sdwan_underlay.txt`, `*_sdwan_overlay.txt`) + debug models and audit reports |
| **Dependencies** | Python 3.10+ standard library only — no pip packages |
| **Connectivity** | `fetch_from_vmanage` uses the **read-only** REST API (GET only, plus the one unavoidable login POST); `convert` is **fully offline** (local file I/O only) |

## Quick Start

```bash
# 1. No install needed (stdlib only)
pip install -r requirements.txt          # no-op

# 2. Fetch the model from vManage over the read-only REST API
#    (password via the VMANAGE_PASSWORD env var)
VMANAGE_PASSWORD=... python -m sdwan_converter.src.fetch_from_vmanage \
    --host vmanage.example.com:8443 --user admin \
    --out sdwan_converter/Input_data/vmanage_export.json

# 3. Convert offline into both diagrams (no vManage connection)
python -m sdwan_converter.src.convert \
    -i sdwan_converter/Input_data/vmanage_export.json \
    -m both \
    -o sdwan_converter/Output_data/ns_commands.txt \
    -c sdwan_converter/sdwan_to_ns_config.json
```

Steps 2 and 3 are **independent**: fetch once, convert as many times as you
like (different `--mode` / config) with no further vManage access. A bundled,
synthetic sample (`Input_data/sample_sdwan_export.json`) lets you try step 3
immediately without any vManage at all. A second bundled sample,
`Input_data/sample_sdwan_export_with_bfd.json`, additionally includes a
synthetic `bfd_sessions` block (Full Mesh on both colours) so you can see the
**underlay's** mesh-shape annotation (see "Underlay mesh-shape annotation"
below) without a live vManage — the base sample has no `bfd_sessions`, so its
transport-segment clouds carry no mesh-shape suffix. Conversely the base
sample is the one that carries `site-name`, `vpn_names` and a full set of
Service VPN interfaces (with the Guest VPN on the branch edges only, as in a
real fabric), so use it for the overlay **and for the underlay's Service VPN
LAN side**: the `_with_bfd` variant deliberately has none of them (just one
unnamed VPN 10 interface, on a single Edge), and therefore falls back to
`site<id>` area names, `VPN <id>` cloud labels and `vpn<id>` VRFs in **both**
diagrams — a useful check of those fallbacks.

## Modes (`--mode`, default `both`)

| `--mode` | What it draws | Built from |
|----------|---------------|-----------|
| `underlay` | vManage / vSmart / vBond + every SD-WAN Edge, plus its VPN0 **physical** interfaces' L1 links — a real device-to-device link wherever OBSERVED LLDP/CDP neighbor data resolves one, otherwise **inferred** gray clouds named after the transport colour (`biz-internet`, `mpls`) — one per TLOC COLOUR of the remaining interfaces, regardless of IPv4 subnet (a star per colour, not a mesh; see "Underlay: one transport cloud per colour" below). Each cloud's label gains a `(Full Mesh)` / `(Hub-and-Spoke)` / `(Partial Mesh)` suffix when observed BFD session data is available (see below). Every cloud also carries ONE L2 segment named after its transport colour on its own `Dummy N` ports (see "Underlay: why the L2 segment is cloud-side only" below) — that is what makes the Underlay L2/L3 diagrams show one broadcast domain per transport — plus one synthesised `Dummy 0` SVI self-bound to that same segment (no L1 link, no IP). Controllers sit in the TOP area row, drawn **and connected** through two synthetic hops that stand in for the routed distance vManage does not expose: a `Dummy_l2` switch carrying all of them on one `dummy_l2` L2 segment, and below it a `Dummy_l3` router linked down to the ONE transport cloud their OBSERVED control connections most point at. The hops carry no IP; each controller's own VPN 0 port carries its reported address but **no** VRF, being a Server-role appliance (see "Underlay: the controllers' control-plane path" below). **Below** each Edge the underlay additionally draws its real Service VPN LAN side — physical VRF interfaces linked down to a synthetic per-Edge `Dummy_<n>` switch, plus its Service VPN Loopbacks, all carrying their IP and their VRF, while the VPN0 transport ports above carry the `Transport_vpn0` VRF (see "Underlay: the Service VPN LAN side" and "Underlay: the VPN 0 transport VRF" below) | `/dataservice/device`, `/dataservice/device/interface`, `/dataservice/device/control/waninterface`, `/dataservice/device/lldp/neighbors`, `/dataservice/device/cdp/neighbors`, plus (optional) `/dataservice/device/bfd/sessions`, `/dataservice/device/control/synced/connections` (or the live `/dataservice/device/control/connections`) and `/dataservice/device/control/connectionshistory` |
| `overlay` | Only those SD-WAN Edges that have at least one **Service VPN** (VRF) interface, each split into a WAN side and a LAN side (see "Overlay: WAN side up, LAN side down" below): a synthetic `Vpn <id>` port per Service VPN links UP to ONE purple waypoint cloud per VPN (a star per VPN, never Edge-to-Edge, with the VPN name as an L2 segment on the **cloud side only**, plus one synthesised `Dummy 0` SVI self-bound to that same segment), while each REAL physical VRF interface keeps its reported IPv4 address and links DOWN to that Edge's own synthetic `Dummy_<n>` LAN switch. Every port on both sides gets the VPN's VRF (`Corporate_vpn10`) as its L3 instance. Service VPN **Loopbacks** are drawn as unconnected virtual ports (IP + VRF only) | `/dataservice/device`, `/dataservice/device/interface` filtered to non-reserved `vpn-id`, plus (optional) `/dataservice/template/feature` for the VPN names |
| `both` | `underlay` + `overlay` (default) | — |

### Underlay L1 links: observed vs. inferred

The underlay draws each SD-WAN Edge's VPN0 **physical** interface as an L1
link in one of two ways, preferring the first that resolves:

1. **OBSERVED** — if vManage's LLDP/CDP neighbor endpoints
   (`/dataservice/device/lldp/neighbors`, `/dataservice/device/cdp/neighbors`)
   report a neighbor for that port that resolves to another SD-WAN-managed
   device, a real device-to-device link is drawn with both real port names
   (LLDP preferred over CDP when both report the same port, since LLDP is
   universal and CDP is IOS-XE/cEdge-only).
2. **INFERRED** (fallback) — for every VPN0 physical interface not resolved
   by step 1, interfaces sharing a **transport colour** are grouped into one
   gray cloud named after that colour (a star per group). See "Underlay:
   one transport cloud per colour" below.

> [!IMPORTANT]
> This has been validated end-to-end **only in inference-fallback mode**.
> The one live vManage this converter has been tested against (a
> CML-simulated lab) does not expose LLDP/CDP neighbor data over its REST
> API at all, so every underlay diagram produced so far — including the one
> from the bundled sample — uses the INFERRED path exclusively. The OBSERVED
> path is implemented against a best-effort, unconfirmed field-name
> convention (see "Accuracy caveats" below) and should be checked against a
> real physical vManage's actual response the first time you rely on it
> there. Set `enable_observed_l1_links` to `false` in the configuration to
> force the inference-only behaviour unconditionally.

### Underlay mesh-shape annotation

When the export includes OBSERVED live BFD tunnel-session state
(`bfd_sessions`, OPTIONAL — see `fetch_from_vmanage.py` below), each inferred
transport segment's cloud label gains a parenthetical suffix — `Full Mesh`,
`Hub-and-Spoke`, or `Partial Mesh` — computed by
`sdwan_topology.classify_mesh_shape`. It answers "*how densely does the
SD-WAN fabric actually mesh across the Edges attached to this circuit?*" from
those Edges' `state: up` BFD sessions (excluding same-site Edge pairs, which
were observed to not form a colour-specific BFD adjacency to each other in
this converter's live validation environment):

- **Full Mesh** — every distinct-site pair of member Edges has an observed
  `up` session on that colour.
- **Hub-and-Spoke** — a small subset of "hub" Edges each connect to every
  other member Edge, while every remaining "spoke" Edge connects ONLY to the
  hub(s) — never directly to another spoke.
- **Partial Mesh** — anything else (some sessions observed, but neither of
  the above patterns).
- **No suffix at all** — no BFD session data for that segment's Edges (e.g.
  the base bundled sample, or an export from `fetch_from_vmanage.py`
  predating this feature), **or** the segment has fewer than 3 member Edges
  (a "mesh shape" is meaningless for a single pair). This mirrors the
  LLDP/CDP-based L1-link inference's own "no data → no annotation" fallback.

The classification is scoped to the **members of that one segment**, not to
every Edge using the colour fabric-wide — one transport colour routinely
spans several independent circuits (the bundled sample has two `biz-internet`
subnets), and classifying colour-wide would mislabel them.

This is an ANNOTATION on the label only — the star drawing (Edge port →
segment cloud) never changes, and the actual Edge-to-Edge mesh is still never
drawn as links. vManage's centralized-policy APIs
(`/dataservice/template/policy/definition/mesh` / `hubandspoke` / ...) were
evaluated as an alternative data source but rejected: in this converter's
live validation environment every vSmart policy was inactive
(`isPolicyActivated: false`) and the one existing "control" policy targeted
a Service VPN, not VPN0 — so policy definitions do not reliably reflect the
actual live mesh shape, while observed BFD session state does.

### Underlay: why the L2 segment is cloud-side only

Each inferred transport cloud carries exactly **one** `add l2_segment_bulk`
entry per `Dummy N` port, named after the **transport colour**
(`biz-internet`, `mpls`) — and **no** L2 segment is placed on the Edge's
`GigabitEthernet N` port, or on any device other than the cloud itself. (The
cloud's own `Dummy 0` SVI is self-bound to that same segment — see "Both
modes: every WayPoint cloud carries a `Dummy 0` SVI" below.)

The segment is what makes the Underlay's L2 and L3 diagrams meaningful at all.
Without it, each Edge↔cloud link is its own two-node broadcast domain, so the
L3 diagram has no subnet to draw and comes out **empty**. With it, every Edge
attached to one colour lands in a single L3 broadcast domain — for the bundled
sample, one domain per transport cloud: all four Edges on `Gi 1` for
`biz-internet` (carrying both `172.16.1.0/24` and `192.0.2.0/29`), and all
four on `Gi 2` for `mpls` (`172.16.2.0/24`).

Keeping it on the cloud side only is the same **asymmetric-L2** pattern the
overlay uses, for the same hard reason: in Network Sketcher **any port that
carries an L2 segment is treated as an L2 switchport and is removed from the L3
interface table**, so segmenting the Edge's physical WAN port would make its
`add ip_address_bulk` fail with `L3 interface not found` and silently strip its
IP address. The Edge's VPN0 interface really *is* a routed L3 port, so it stays
segment-free; the cloud's internal `Dummy N` ports are what need grouping.

The segment name is the colour, and so is the cloud's own name: since the
merge there is exactly **one cloud per colour**, so the segment name is
unambiguous by construction — even for a cloud spanning several access
subnets, which is one broadcast domain by design.

### Underlay: the Service VPN LAN side

The Underlay is **not VPN-0-only**. Above each Edge it draws the VPN0 transport
circuits; **below** it, it draws the very same LAN side the Overlay does — for
`br1-edge1` in the bundled sample:

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
                   10.10.3.1/24       10.11.3.1/24  10.12.3.1/24
                          └────────────────┼─────────────┘
                                    [  Dummy_1  ]                             ← SYNTHETIC, tier row 4
                                   (split by VRF, no IP)
```

Everything below the Edge — the `Dummy_<n>` switch and its numbering, the LAN
links, the IPs, the Loopbacks and every VRF label — is produced by
`sdwan_lan_side`, the **one** module both mappers call, so the two diagrams
place the identical switch under the identical Edge with the identical VRF
name (`Dummy_1` = `br1-edge1`, `Dummy_2` = `br2-edge1`, `Dummy_3` =
`dc1-edge1`, `Dummy_4` = `dc1-edge2` for the bundled sample) and cannot drift
apart. The switches are light gray in **both** diagrams, for the same reason:
they are synthesised placeholders with no vManage counterpart.

What stays exclusive to the Overlay is the Service VPN **cloud abstraction**:
the synthetic `Vpn <id>` membership ports and the per-VPN waypoint clouds are
never drawn in the Underlay. Conversely the transport clouds are never drawn in
the Overlay. The LAN side is the deliberate overlap between the two.

Layout follows RULE 0 (top-to-bottom hierarchy) and RULE 0.5 (L1 crossing
avoidance): transport clouds in `wan_wp_` on their own row at the top, the
Edges on their site area's first row, and the LAN switches on a second row
below them — `add device_location "['br1',[['br1-edge1'],['Dummy_1']]]"`, the
same two-row form the Overlay already emits.

The three configuration keys that shape this side — `excluded_service_vpns`,
`vrf_name_format` and `lan_dummy_name_format` — therefore now apply to **both**
modes, not to the overlay alone. `transport_vpn_name` (below) is
underlay-only.

### Underlay: the VPN 0 transport VRF (`Transport_vpn0`)

Every Edge's VPN0 physical transport interface (`GigabitEthernet 1` /
`GigabitEthernet 2` in the bundled sample) carries **VPN 0 itself as its L3
instance**:

```
rename l3_instance 'br1-edge1' 'GigabitEthernet 1' 'Transport_vpn0'
rename l3_instance 'br1-edge1' 'GigabitEthernet 2' 'Transport_vpn0'
```

so the Underlay's L3 interface table separates the SD-WAN Transport VPN from
the Service VPNs instead of pooling it into an unnamed default. Nothing else
about those ports changes: same IP, same L1 link to the transport cloud.

The name is not hardcoded. It goes through the **same** `vrf_label_for_vpn` /
`vrf_name_format` path as `Corporate_vpn10`, called with vpn-id `0` and the
`transport_vpn_name` configuration value (default `Transport`), so
`{name}_vpn{id}` yields `Transport_vpn0`. Blank `transport_vpn_name` to fall
back to a bare `vpn0`; change `vrf_name_format` and both the transport VRF and
the Service VPN VRFs follow it together.

> [!NOTE]
> The VPN 0 name is deliberately **not** read from the export's own
> `vpn_names`, unlike every Service VPN name. vManage's VPN 0 template is
> usually called something like `VPN0`, which would render as the redundant
> `VPN0_vpn0`; the configuration value gives a stable, readable label instead.

**Edge side only — the transport clouds get no VRF.** No `rename l3_instance`
targets a transport cloud: its `Dummy N` link ports and
its `Dummy 0` SVI carry the transport-colour L2 segment and nothing else. This
mirrors the Overlay exactly, where the Edges and the LAN switches get the VRF
but the `Corporate` / `PCI` / `Guest` waypoint clouds never do — and it is also
what keeps those cloud ports valid L2 switchports for the L2/L3 renderer.

### Underlay: the controllers' control-plane path

The controllers are no longer unconnected inventory nodes. Their control
traffic **is** visible in vManage and it **does** ride the VPN 0 transport this
underlay already draws — but they cannot simply be cabled to a transport cloud,
because they are not on any Edge transport subnet. They are hung off two
synthesised hops instead. The `controllers` area is the **top** row of
`add area_location`, so the path reads downwards into the transport clouds
exactly like an Edge's own uplink; the bundled sample produces exactly this:

```
              [Controller01]   [Manager01]   [Validator01]          ← area controllers (TOP row)
                  vSmart         vManage         vBond
               Ethernet 1     Ethernet 1   GigabitEthernet 0/0        real VPN 0 ports,
              172.16.0.101/24 172.16.0.1/24  172.16.0.201/24          IP only, no VRF
                      |            |             |
                   Dummy 1      Dummy 2       Dummy 3
                       [        Dummy_l2        ]                   ← SYNTHETIC, Switch
                               Dummy 0                                all 4 ports in the
                                  |                                   'dummy_l2' segment
                               Dummy 1
                       [        Dummy_l3        ]                   ← SYNTHETIC, Router
                               Dummy 0                                (no L2 segment, no IP)
                                  |
                               Dummy 5
                       [        mpls          ]                    ← area wan_wp_
```

**What the data says.** `/dataservice/device/control/[synced/]connections`
returns, per Edge, one row per control connection that Edge holds up to a
controller: the controller's `peer-type` and `system-ip`, the **Edge's own**
`local-color`, and the controller's `private-ip`/`public-ip`. That is enough to
say *which* transport circuit carries the control plane. It is **not** enough to
say how far away the controller is: controllers sit on their own subnet, disjoint
from every Edge transport subnet, and `/dataservice/device/ip/routetable` answers
with an empty `data` array, so the number of routed hops is unobtainable. Linking
a controller straight to a transport cloud would therefore assert an adjacency
the data contradicts.

**What is drawn instead.** Two synthesised light-gray devices, stacked **below**
the controllers inside their own area (the area itself sits above the transport
clouds, so the whole construct points downwards):

- **`Dummy_l2`** (`Switch` stencil) stands in for the segment the controllers
  really do share. Every controller links to it **on its own real VPN 0 port**,
  and it carries one L2 segment named `dummy_l2` on **all** of its own ports —
  the link down to `Dummy_l3` included — and on no controller port, the same
  cloud-side-only asymmetry the transport clouds use.
- **`Dummy_l3`** (`Router` stencil) stands in for the unknown routed hop. It is
  linked DOWN to **exactly one** transport cloud — the most likely carrier of
  the control plane, see below — and UP to `Dummy_l2`. It carries no L2 segment,
  which is what makes it read as a router rather than a switchport.

Ports on both devices are pseudo ports numbered from `Dummy 0` upwards. This
deliberately differs from the transport clouds, which reserve `Dummy 0` for
their RULE 15 SVI and start their link ports at `Dummy 1`; there is no SVI here.

**Which cloud, confirmed by address rather than trusted by name.** The row's
`local-color` is resolved through that Edge's `control/waninterface` entries to
its real transport address, and that address is matched against the Edge's
already-resolved VPN 0 ports; the colour of the port it lands on is the cloud
the row votes for. Since the clouds merged by colour, that selection is now a
**colour**, not a colour+subnet — but going through the addresses is still
worth doing: a row naming a colour this Edge has no VPN 0 port for votes for
nothing and is counted as unresolved instead of being taken at face value.

**Exactly ONE uplink, picked by weight of evidence.** The control plane really
does leave the fabric over a single routed path, so linking `Dummy_l3` to every
cloud it was observed over turned it into a hub touching all of them. Instead
the observed groups are ranked and only the strongest is drawn, in this
deterministic order:

| Rank criterion | Why |
|---|---|
| 1. Observed control-connection **rows** | the most direct measure of how much control traffic was seen over that circuit |
| 2. Number of **controllers** reached | a circuit that reaches all three controllers beats one that reaches one |
| 3. Number of **member Edges** on the cloud | a cloud more Edges sit on is the likelier path off the fabric |
| 4. Cloud **name** | tie-break only, so the output is stable |

In the bundled sample `mpls` wins on rows and controllers (8 rows / 2
controllers / 4 member Edges, versus 4 / 1 / 4 for `biz-internet`). The
runner-up is **not** lost: the chosen cloud, its score, and every
observed-but-undrawn cloud with its score go into the underlay report's
caveats.

**The controllers' own VPN 0 ports carry their reported IP address**, so the
segment they share is a real subnet (`172.16.0.0/24` in the sample) in the L3
diagram rather than an L1/L2-only construct. They get **no L3 instance (VRF)**:
a controller is a `Server`-role appliance, not a routing node, and the Transport
VPN is an Edge concept — so they land in Network Sketcher's default instance
while the *Edges'* VPN 0 ports stay in `Transport_vpn0`. The two SYNTHETIC hops
get no IP at all — they stand in for structure that was never measured — and a
controller's **VPN 512** out-of-band port stays excluded. Two consequences worth
knowing: the `controllers` area now carries L3 content, so the L3-less-area
reordering below no longer applies to it, and the NS L3 renderer tints
L3-bearing device labels (see "Known Network Sketcher behaviour: the L3 diagram
tints device labels").

**Per-controller nuance is annotated, not drawn.** Because `Dummy_l3` draws one
uplink, the fact that (say) the vManage is reachable over `mpls` only while the
vSmarts use both colours would otherwise be lost. It is preserved as an
annotation — on each controller (`control plane: mpls`), on `Dummy_l3` itself,
and in full in the underlay report — following the same
precedent as the BFD mesh-shape suffix, which likewise annotates a real but
non-adjacent relationship rather than drawing it.

**The vBond is a special case.** Its control connections are torn down once
onboarding completes, so `control/connections` returns zero rows for it and it
appears only in `control/connectionshistory`, with `state: tear_down` and an
unusable `system-ip` of `0.0.0.0` (joinable to the device by its transport
address alone). It is therefore **drawn on the shared segment** — which is a
fact about its subnet — but contributes **no** uplink and is annotated
`control plane: history only`. A torn-down session is not current connectivity.

**Fallback and gate.** If the export carries no `control_connections` data, or
none of it can be joined to a known controller and an inferred transport
segment, the controllers are drawn exactly as before this feature existed
(unconnected inventory nodes) and the report gets a caveat saying so. No colour
and no address is ever synthesised to bridge the gap. Setting
`enable_controller_control_plane_links` to `false` forces that same behaviour
unconditionally: the `controllers` area falls back to its single-row
`add device_location "['controllers',[['Controller01','Manager01','Validator01']]]"`
and no `Dummy_l3` / `Dummy_l2` / `dummy_l2` line is emitted at all — and with no
L1 link, no controller IP either. The area is still promoted to the top row
(that is pure layout, see the next section).

> [!NOTE]
> **Viptela-OS vs IOS-XE interface schema.** `/dataservice/device/interface`
> answers in two different shapes depending on the queried platform's OS, and a
> controller is always Viptela-OS even in a fabric whose Edges are all IOS-XE
> cEdges:
>
> | | IOS-XE (cEdge) | Viptela-OS (controllers, vEdge) |
> |---|---|---|
> | `ifname` | `GigabitEthernet1` | `eth1`, `ge0/0` |
> | Prefix length | separate `ipv4-subnet-mask` field | inside `ip-address` (`172.16.0.1/24`), **no** mask field |
>
> Both forms are accepted on the controller path
> (`sdwan_topology.controller_vpn0_interfaces` / `interface_cidr`), and
> `normalise_port_name` renders `eth1` → `Ethernet 1` and `ge0/0` →
> `GigabitEthernet 0/0`. A controller's **VPN 512** port (vManage's `eth0`) is
> excluded by the VPN 0 filter, as it should be: *that* is the genuine
> out-of-band management network — the control traffic itself is not
> out-of-band.

### Underlay: the controllers' area is the top row

`build_area_layout` puts every non-waypoint area on **one** row below the
waypoint row, which left the controllers beside the sites and their
control-plane path climbing back **up** over the transport clouds. The underlay
lifts them out of that row into a row of their own at the very top, giving a
three-row layout:

```
add area_location "[['controllers'],['wan_wp_'],['br1','br2','dc1']]"
```

and the order **inside** the area is reversed to match — controllers on top,
then `Dummy_l2`, then `Dummy_l3` nearest the clouds it links down to:

```
add device_location "['controllers',[['Controller01','Manager01','Validator01'],['Dummy_l2'],['Dummy_l3']]]"
```

Everything then points downwards, the way each Edge's own transport uplink
already does (NS RULE 0). This is a post-pass
(`_promote_controller_area_to_top`) applied right after the L3-less reordering
below, exactly so the shared `ns_model.build_area_layout` helper — used by every
converter in this repo — stays untouched. The rule is driven by device
personality, not by a hardcoded area name, and an area that also holds an Edge
(a controller sharing a `site-id` with one) is left where the layout helper put
it, so lifting the controllers can never drag a site out of the site row.

#### Known limitation: the converter cannot choose an area's column

`add area_location` emits a **grid of rows**, and that is the whole of its
authority: it fixes which row an area sits on and the order of the areas within
that row. The **column** each row starts in is computed by Network Sketcher and
cannot be influenced by the emitted script. In the bundled sample this means the
`controllers` row starts one column to the *left* of `wan_wp_`:

```
All Areas Position (bundled sample, two transport colours)

controllers |         |     |     |     |
            | wan_wp_ |     |     |     |
            | br1     |     | br2 |     | dc1
```

The mechanism is in `recalculate_folder_sizes()`, the pass the engine runs at
the end of **every** layout or device mutation. It gives each row a
`total_width` — the sum of the rendered widths of that row's areas, with empty
cells explicitly *excluded* from the sum — finds the single widest row, strips
that row's leading empty cell so it starts at column 1, and forces a leading
empty cell onto every other row so they start at column 2. Because empty cells
contribute nothing to `total_width`, no arrangement of `''` cells can move a row.
Measured against the NS engine on the built sample master, these four forms all
produced the **identical** `POSITION_FOLDER`:

| emitted form | result |
| --- | --- |
| `[['','controllers'],['wan_wp_'],[…]]` (leading spacer) | `controllers` col 1 |
| `[['controllers','','','',''],['wan_wp_','','','',''],[…]]` (pad every row to the widest row's element count) | `controllers` col 1 |
| `[['controllers'],['','wan_wp_'],[…]]` (spacer on the waypoint row) | `controllers` col 1 |
| `[['','','controllers',''],['','wan_wp_',''],[…]]` (leading + trailing on every row) | `controllers` col 1 |

Padding every row to the widest row's element count is the form that looks most
promising, since `make_position_folder_tuple` only injects side padding into
rows that are not the longest (`needs_side_empty = (row_idx != max_row_idx)`).
It does not work: that function calls `remove_trailing_empty_cells()` on each
row *before* it measures them, so the padding is gone by the time
`max_row_idx` is computed, and `recalculate_folder_sizes()` then overrides the
result on width anyway. There is also no other lever — `add area_location` is
the only CLI verb that touches the area grid at all; nothing sets an area's
column, width, or offset.

What actually decides the outcome is how many devices each area holds. Three
runs of the **same** emitted command against masters built from one, two and
three transport colours:

| transport colours | `wan_wp_` width | `controllers` width | widest row | resulting grid |
| --- | --- | --- | --- | --- |
| 1 | 3.7601 | 5.5392 | `controllers` | `controllers` col 1, `wan_wp_` col 2 |
| 2 (bundled sample) | 5.5073 | 5.5392 | `controllers` | `controllers` col 1, `wan_wp_` col 2 |
| 3 | 7.5073 | 5.5392 | `wan_wp_` | `wan_wp_` col 1, `controllers` col 2 |

The three-colour fabric flips the arrangement purely because the waypoint row
finally out-grows the three side-by-side controllers — and in the two-colour
sample the two rows are within 0.03 inch of each other, so the flip is decided
by a 0.6 % width difference. Earlier versions of this converter emitted a
leading `''` spacer on the controllers row and re-applied the line as a final
`# Phase: 6 area_location (re-apply)` command; both have been removed, because
the measurements above show the spacer changes nothing and the re-apply had
nothing left to restore.

None of this changes what the diagram says. The controllers still occupy their
own row above the transport clouds, with their control-plane path running
downwards, which is the relationship the drawing is read for; only the
horizontal offset of that row varies with the fabric.

One reading note: `show area_location` strips empty cells when it prints, so it
is not a reliable way to inspect the grid. Read `POSITION_FOLDER` or the Device
Table's *All Areas Position* tab instead.

### Underlay: areas with no L3 interface sort last

Within a row of `add area_location`, any area that holds **no IP-bearing (L3)
interface** is emitted **last**, with the remaining areas keeping their normal
order — `[['br1','br2','dc1','site9']]` rather than
`[['br1','br2','site9','dc1']]`.

This is a **workaround for an upstream Network Sketcher engine bug**, not a
layout preference. The all-areas L3 renderer's `calculate_area_offset()`
positions each area at the sum of the widths of the areas *preceding* it in its
row, but its width lookup only contains areas that actually carry L3 content —
so an L3-less area sitting anywhere but last aborts the whole L3 export with
`KeyError: '<area>'`. A row that is L3-less **end to end** is left alone: the
renderer never looks it up, which is why the `wan_wp_` waypoint row is safe (and
why the `controllers` row was safe before the controllers' VPN 0 addresses were
assigned; today that area carries L3 content like any other).

The rule is general (any L3-less area sorts last, no hardcoded area name).
The **overlay** does not need it: an Edge is only drawn there if it has at
least one Service VPN interface *with a usable IPv4 address*, so every overlay
site area necessarily carries L3 content.

### Both modes: every WayPoint cloud carries a `Dummy 0` SVI

In **both** diagrams, each WayPoint cloud gets one extra port called exactly
`Dummy 0`, modelled as an **SVI** (`add virtual_port_bulk`) and **self-bound**
to that cloud's own L2 segment (`add l2_segment_bulk`) — Network Sketcher's
RULE 15 "mandatory SVI-to-L2-segment binding" pattern:

| | Underlay | Overlay |
|---|---|---|
| Cloud | one per transport colour (`biz-internet`) | one per Service VPN (`Corporate`) |
| Segment it binds to | the transport colour (`biz-internet`, `mpls`) | the VPN label (`Corporate`, `PCI`, `Guest`) |

It is bound to the **same segment name** the cloud's `Dummy 1`…`Dummy N` link
ports already carry, so the SVI and those ports can never drift apart. The
port has **no L1 link** and **no IP address**: it exists only to give the
segment an L3/SVI anchor on the cloud side. Like everything else on these
clouds it is **synthesised** — there is no `Dummy 0` interface, and no cloud,
anywhere in the vManage data. In the underlay the missing IP is also what lets
a transport cloud span several access subnets: see "Underlay: one transport
cloud per colour".

`Dummy 0` cannot collide with the cloud's L1 link ports, which are numbered
from `Dummy 1` upwards in both mappers. Both mappers count the SVIs
(`cloud_svi_ports`) and list them under `SYNTHETIC` in their report.

The emitted script places the `add virtual_port_bulk` phase **before** the
`add l2_segment_bulk` phase, since the port must exist before it can be
bound; the file still applies cleanly top-to-bottom. This is the first time
the **underlay** emits a `virtual_port_bulk` phase at all — its phases are now
1, 2, 2.5, **3 (virtual_port_bulk)**, 3 (l2_segment_bulk), 4 (ip_address_bulk),
4 (l3_instance), 6. In the underlay that same `virtual_port_bulk` phase now
also carries the Service VPN Loopbacks (see "Underlay: the Service VPN LAN
side" below); unlike the `Dummy 0` SVIs, those are real interfaces and do get
an IP and a VRF.

### VPN0, Tunnel<N> and the Service VPNs: what maps where

**VPN 0 is a routing context (the Transport VPN), not a single interface.**
Every VPN0-tagged interface on an Edge — physical AND logical alike — is
just a member of that global routing table; there is no one interface that
"is" VPN0. Everything with a *non-reserved* `vpn-id` (10, 11, 12, ...) is
instead a member of a **Service VPN**: an independent customer VRF. Each
routing context is drawn as its own `l3_instance`, confirmed against Cisco's
SD-WAN documentation and a live vManage:

| Interface | `vpn-id` | Real / virtual | This converter draws it in... |
|---|---|---|---|
| `GigabitEthernetN` / `TenGigabitEthernetN` / ... | `0` | Real physical port | **Underlay only** (`sdwan_physical_mapper.py`) — the WAN transport circuit: L1 link to its transport cloud + IP + the `Transport_vpn0` VRF on the Edge port |
| `GigabitEthernetN` / ... (incl. dot1q sub-interfaces) | Service VPN (10, 11, ...) | Real physical port | **Both diagrams** — in the overlay an L1 link UP to its VPN's cloud (+ L2 segment on the cloud side only), in the underlay an L1 link DOWN to the Edge's LAN switch; IP + VRF on the Edge port either way |
| `LoopbackN` | Service VPN | Real logical interface | **Both diagrams**, as an UNCONNECTED virtual port (IP + VRF only, no L1/L2) — see "Loopbacks" below |
| `TunnelN` | `0` | **Real** logical interface, auto-created by IOS-XE (cEdge) when a physical VPN0 interface has `tunnel-interface`/`encapsulation` enabled — it genuinely appears in `show interface` and vManage's own interface list | **Neither diagram.** It is bound to exactly one physical interface via `tunnel source <physical-if>` and shares that interface's colour and IPv4 address, so drawing it would only duplicate the underlay's physical port |
| `NVI0`, `Sdwan-system-intf`, `vmanage_system`, `LoopbackN` in VPN0 | `0` | Pseudo/system interfaces (control-plane plumbing, no transport role) | **excluded from both** diagrams |
| anything in VPN `512` / `65528`–`65530` | reserved | out-of-band management / internal | **excluded from both** diagrams, unconditionally |

## Output files

| File | Mode | Description |
|------|------|-------------|
| `<stem>_sdwan_underlay.txt` | underlay | NS CLI commands (Phase 1–6) for the physical/circuit view, incl. the cloud-side L2 segments, every Edge port's IP, and the Service VPN LAN side below each Edge (LAN switches, Loopbacks and their VRFs) |
| `<stem>_sdwan_overlay.txt` | overlay | NS CLI commands (Phase 1–6) for the Service VPN overlay |
| `ns_model_underlay.json` / `ns_model_overlay.json` | each | Intermediate topology model (debug) |
| `sdwan_inventory.csv` | underlay | Device inventory + stencil mapping (audit) |
| `sdwan_underlay_report.md` / `sdwan_overlay_report.md` | each | Counts + accuracy caveats (incl. every inferred/synthesised element) |

> [!NOTE]
> **Behaviour change — the underlay is no longer L1-only.** It now also emits
> one L2 segment per transport cloud (cloud side only, named after the
> transport colour) so the Underlay L2/L3 diagrams are populated instead of
> empty, and it sorts L3-less areas to the end of the area row to avoid an NS
> engine crash. Both are documented below ("Underlay: why the L2 segment is
> cloud-side only", "Underlay: areas with no L3 interface sort last"). The
> underlay command file was accordingly renamed from
> `<stem>_sdwan_underlay(L1Only).txt` to `<stem>_sdwan_underlay.txt`.
> Everything else the underlay emitted before — devices, L1 links, port info,
> the Edge ports' `add ip_address_bulk` entries and the attribute sheet — is
> byte-for-byte unchanged, and the overlay is unaffected.

> [!NOTE]
> **Behaviour change — a `Dummy 0` SVI on every WayPoint cloud, and gray
> overlay LAN switches.** Two additions, in both cases affecting only lines
> that did not exist before:
>
> - **Both modes** now emit one `Dummy 0` SVI per WayPoint cloud, self-bound
>   to that cloud's existing L2 segment (the transport colour in the underlay,
>   the VPN label in the overlay), with no L1 link and no IP — see "Both
>   modes: every WayPoint cloud carries a `Dummy 0` SVI" above. The underlay
>   therefore emits an `add virtual_port_bulk` phase for the first time,
>   ordered before `add l2_segment_bulk`.
> - **Overlay only:** the synthetic `Dummy_<n>` LAN switches are now light
>   gray `[200, 200, 200]` in `rename attribute_bulk` instead of the overlay's
>   light purple `[221, 204, 255]`, matching the shared palette's meaning for
>   a synthesised placeholder. The Edges and the Service VPN clouds stay light
>   purple.
>
> Nothing else changed in either command file: the areas, device placement, L1
> links, port info, existing L2 segments, IP addresses and VRF renames are all
> byte-for-byte identical.

> [!NOTE]
> **Behaviour change — the underlay now draws the Service VPN LAN side, puts
> every Edge port in a VRF, and connects the controllers.** The Underlay is no
> longer VPN-0-only, no longer VRF-less, and no longer leaves the controllers
> floating:
>
> - **Below each Edge** it now draws the SAME LAN side the overlay does — the
>   same `Dummy_<n>` LAN switches (same names, same Edge→switch assignment),
>   the Edge's physical Service VPN interfaces linked down to them with their
>   IPs, its Service VPN Loopbacks as unconnected virtual ports, and the same
>   `<VPN name>_vpn<id>` VRFs on all of those ports. Both mappers now call one
>   shared helper (`src/sdwan_lan_side.py`) for that side, so the two diagrams
>   cannot drift apart — see "Underlay: the Service VPN LAN side" above.
> - **Above each Edge** its VPN0 physical transport interfaces
>   (`GigabitEthernet 1` / `GigabitEthernet 2`) now carry the
>   **`Transport_vpn0`** VRF, built from vpn-id 0 and the new
>   `transport_vpn_name` config key through the same `vrf_name_format` path as
>   `Corporate_vpn10`. Their IP and their L1 link to the transport cloud are
>   unchanged. The transport CLOUDS get no VRF — `rename l3_instance` is Edge
>   side only, mirroring the overlay's waypoint clouds. See "Underlay: the
>   VPN 0 transport VRF" above.
> - **In the `controllers` area** the controllers are no longer unconnected:
>   two synthetic gray hops, `Dummy_l3` (Router) above `Dummy_l2` (Switch),
>   now carry them up to every transport cloud their OBSERVED control
>   connections were resolved to — by address, through
>   `control/waninterface`, never by colour name. Every controller links to
>   `Dummy_l2` on its own real VPN 0 port and shares one `dummy_l2` L2 segment
>   bound to the `Dummy_l2` side only. **No IP is assigned anywhere in this
>   construct**, so the area stays L3-less and keeps sorting last *(both
>   superseded by the next note: the area now sits on top, and the controllers
>   carry their VPN 0 addresses)*. Per-controller
>   colour nuance is annotated, not drawn. See "Underlay: the controllers'
>   control-plane path" above; set `enable_controller_control_plane_links` to
>   `false` to restore the previous unconnected-controllers output.
> - The underlay emits a `rename l3_instance` phase for the first time; its
>   phases are now 1, 2, 2.5, 3 (`virtual_port_bulk`), 3 (`l2_segment_bulk`),
>   4 (`ip_address_bulk`), **4 (`l3_instance`)**, 6.
> - The underlay's `add device_location` now uses the multi-row form
>   (`"['br1',[['br1-edge1'],['Dummy_1']]]"`, and three rows for
>   `controllers`), and `add l1_link_bulk`, `add virtual_port_bulk`,
>   `add ip_address_bulk`, `add l2_segment_bulk`, `rename port_info_bulk` and
>   `rename attribute_bulk` each gained new entries. `add area_location` is
>   byte-for-byte unchanged, as are every existing WAN L1 link, WAN IP address
>   and transport-colour segment — the transport clouds, their cloud-side
>   colour segments and the `Dummy 0` SVIs are untouched apart from the extra
>   `Dummy N` port each cloud lends to the `Dummy_l3` uplink.
> - `excluded_service_vpns`, `vrf_name_format` and `lan_dummy_name_format`
>   now affect **both** modes rather than the overlay alone;
>   `transport_vpn_name`, `enable_controller_control_plane_links`,
>   `control_l3_dummy_name` and `control_l2_dummy_name` are new and
>   underlay-only.
>
> **The overlay is completely unaffected** — its command file is byte-for-byte
> identical to before this change.

> [!NOTE]
> **Behaviour change — the controllers move to the top, get their IP addresses,
> and hang off ONE transport cloud.** Three underlay changes, all in the
> `controllers` area:
>
> - **`add area_location` is now three rows** —
>   `[['controllers'],['wan_wp_'],['br1','br2','dc1']]` — and the order inside
>   the area is reversed (controllers, then `Dummy_l2`, then `Dummy_l3`), so the
>   control-plane path points DOWN into the transport clouds like every Edge
>   uplink. See "Underlay: the controllers' area is the top row" above.
> - **Each controller's own VPN 0 port now carries its reported IP address**
>   (`Manager01` `Ethernet 1` `172.16.0.1/24`, …), so the segment they share is a
>   real subnet in the L3 diagram. No VRF is written on it — a controller is a
>   `Server`-role appliance, not a routing node — so it lands in Network
>   Sketcher's default L3 instance while the Edges' VPN 0 ports stay in
>   `Transport_vpn0`. VPN 512 stays excluded; the two synthetic hops still carry
>   no IP. The `controllers` area therefore has L3 content now and no longer
>   sorts last.
> - **`Dummy_l3` links to exactly ONE transport cloud** — the most likely
>   carrier, ranked by observed control-connection rows, then controllers
>   reached, then member Edges, then name — instead of to every cloud it was
>   observed over. `mpls` in the bundled sample. The undrawn clouds and the
>   winning score are reported in the underlay report's caveats.
>
> **The overlay is completely unaffected** — its command file is byte-for-byte
> identical (SHA-256 `e4d9103a…1331b7`).

---

## vManage → Network Sketcher mapping

### Underlay
| vManage | NS |
|------|-----|
| `personality` `vmanage` / `vsmart` / `vbond` | `Server` device (cloud/VM-hosted control-plane appliance, not data-plane forwarding — mirrors `aci_converter`'s APIC→Server and `catc_converter`'s controller→Server precedent) |
| `personality` `vedge` (cEdge/vEdge data plane) | `Router` |
| `device-model` (used to infer `personality` when the field is absent) | lower-confidence fallback (0.85 vs. 1.00) — flagged in `sdwan_inventory.csv` |
| `site-name` (falling back to `site-id`) | an **area** — `br1`, else `site<id>`; see "Area naming: `site-name` first" below |
| VPN0 **physical** interface (`Gi`/`Te`/... ; Tunnel and other VPN0 pseudo-interfaces like `NVI0`/`Sdwan-system-intf`/`Loopback*` excluded) whose LLDP/CDP neighbor resolves to another SD-WAN-managed device (`lldp/neighbors`, `cdp/neighbors`) | a real **OBSERVED** L1 link between the two real devices, with both real port names (preferred over the inferred grouping below; LLDP wins over CDP on a shared port) |
| Any remaining VPN0 **physical** interface with a real IPv4 address, grouped by its **transport colour alone** (the IPv4 subnet is NOT part of the key) | one gray **INFERRED** cloud named after the colour (`biz-internet`) per group; every member Edge links to it with its real port + IP (a star), so one cloud can carry several access subnets. A colour with only one member still gets its own single-member cloud (that Edge's dedicated WAN circuit). See "Underlay: one transport cloud per colour" below |
| `control/waninterface` `color` of the group above | an `add l2_segment_bulk` entry named after the colour (`biz-internet`) on **every `Dummy N` port of that cloud and on no other port** — one L3 broadcast domain per transport (see "Underlay: why the L2 segment is cloud-side only" below) |
| *(nothing — SYNTHESISED)* | one `Dummy 0` SVI per transport cloud (`add virtual_port_bulk`) self-bound to that cloud's colour segment (`add l2_segment_bulk`), with no L1 link and no IP — see "Both modes: every WayPoint cloud carries a `Dummy 0` SVI" above |
| `control/waninterface` `color` matched by `private-ip`/`public-ip` | resolves the transport colour for a VPN0 IP; unresolved matches fall back to `unknown_color_label` (config) |
| Live BFD session state (`/dataservice/device/bfd/sessions`), OPTIONAL | used ONLY to compute each cloud's mesh-shape label suffix (see "Underlay mesh-shape annotation" above) — never drawn as an Edge-to-Edge link |
| VPN0 **physical** interface, as an L3 instance | `rename l3_instance` setting `Transport_vpn0` — VPN 0 fed through the same `vrf_name_format` path as every Service VPN, with its display name from `transport_vpn_name` (see "Underlay: the VPN 0 transport VRF" above). Edge side only: the transport clouds get **no** VRF |
| Each Edge with ≥1 Service VPN **physical** interface (LAN side, **SYNTHETIC**) | one `Dummy_<n>` LAN switch (`Switch` stencil, **gray**) in the SAME area as its Edge, on the Access tier row below it — the SAME switch, with the same name and the same Edge, that the overlay draws (shared `sdwan_lan_side` helper). Its pseudo ports report `Unknown` speed/duplex/media |
| Service VPN **physical** interface (`GigabitEthernet 3`, incl. dot1q sub-interfaces) with a real IPv4 address | an L1 link (real port name) DOWN to that Edge's `Dummy_<n>` LAN switch on a `Dummy N` pseudo-port, the reported IP/mask via `add ip_address_bulk`, and `rename l3_instance` setting the VPN's VRF (`Corporate_vpn10`) on **both** ends. No L2 segment on either end |
| Service VPN **Loopback** interface | `add virtual_port_bulk` + `add ip_address_bulk` + `rename l3_instance` ONLY — no L1 link, no L2 segment (NS RULE 15's Loopback exception) |
| Each Edge's control connections (`control/[synced/]connections`, OPTIONAL), joined **by address** through `control/waninterface` to a transport colour | ONE L1 link from the synthetic `Dummy_l3` DOWN to the single most likely of those transport clouds (ranked by observed rows, controllers reached, member Edges, then name), on the next free `Dummy N` port of the cloud (which joins that cloud's existing colour segment). Rows that are down, name an unknown peer, or resolve to no inferred cloud are counted and skipped; clouds that lose the ranking are reported, not drawn |
| *(nothing — SYNTHESISED)* | `Dummy_l2` (`Switch` stencil, **gray**, tier row below the controllers) standing in for the segment they share, and `Dummy_l3` (`Router` stencil, **gray**) below it standing in for the unknown routed hop. Pseudo ports numbered from `Dummy 0`; **no IP on either** — see "Underlay: the controllers' control-plane path" above |
| Every controller's own VPN 0 port (`eth1`, `ge0/0`, `GigabitEthernet1`, ... — Viptela-OS and IOS-XE shapes both accepted) | an L1 link with that real port name DOWN from the controller to `Dummy_l2`, an `add l2_segment_bulk` entry named `dummy_l2` on **the `Dummy_l2` side only**, and the port's reported IP via `add ip_address_bulk`. **No** `rename l3_instance`: a controller is a `Server`-role appliance, not a routing node, so it stays in the default L3 instance |
| A controller with no CURRENT control connection (a vBond after onboarding — history only, `state: tear_down`, `system-ip 0.0.0.0`) | still drawn on `Dummy_l2`, but contributes no uplink; annotated `control plane: history only` |
| Controller VPN **512** port (vManage's `eth0`) | **NOT drawn** — the genuine out-of-band management network, separate from the transport fabric |
| The Service VPN CLOUD abstraction (`Vpn <id>` membership ports, per-VPN waypoint clouds) | **excluded** here — that is the **overlay's** subject |

### Overlay
| vManage | NS |
|------|-----|
| SD-WAN Edge (`personality vedge`) with ≥1 Service VPN interface — controllers, and Edges with none, are NOT drawn | `Router` device, coloured purple, on the top tier row of its site area |
| Each distinct non-reserved `vpn-id` seen on any Edge | one purple **Service VPN** waypoint cloud (`Cloud` stencil) in the shared `overlay_wp_` area, labelled with the VPN's configured NAME (`Corporate`) or `VPN <id>` when unresolved |
| `vpn_names` (from `/dataservice/template/feature`), OPTIONAL | resolves each `vpn-id` to that cloud's display name and to the VRF name — see "Service VPN names" below |
| Each Service VPN an Edge participates in (WAN side, **SYNTHETIC**) | a `Vpn <id>` port on the Edge, L1-linked UP to that VPN's cloud on a `Dummy N` pseudo-port, with the VPN name as an `add l2_segment_bulk` entry on the **cloud's `Dummy N` port ONLY** (see "why the L2 segment is cloud-side only" below) and `rename l3_instance` setting the port's VRF. **No IP** — it is not a real interface |
| *(nothing — SYNTHESISED)* | one `Dummy 0` SVI per Service VPN cloud (`add virtual_port_bulk`) self-bound to that cloud's VPN segment (`add l2_segment_bulk`), with no L1 link and no IP — see "Both modes: every WayPoint cloud carries a `Dummy 0` SVI" above |
| Each Edge with ≥1 Service VPN **physical** interface (LAN side, **SYNTHETIC**) | one `Dummy_<n>` LAN switch (`Switch` stencil, **gray** — a synthesised placeholder) in the SAME area as its Edge, on the Access tier row below it. Numbered globally in sorted Edge-name order; an Edge with only Loopbacks gets none |
| Service VPN **physical** interface (`GigabitEthernet 3`, incl. dot1q sub-interfaces) with a real IPv4 address | an L1 link (real port name) DOWN to that Edge's `Dummy_<n>` LAN switch on a `Dummy N` pseudo-port, the reported IP/mask via `add ip_address_bulk`, and `rename l3_instance` setting the VPN's VRF on **both** ends of the link (the Dummy side gets the VRF but no IP). No L2 segment on either end |
| Service VPN **Loopback** interface | `add virtual_port_bulk` + `add ip_address_bulk` + `rename l3_instance` ONLY — no L1 link, no L2 segment (see "Loopbacks" below) |
| VPN 0 (physical, Tunnel, pseudo), transport colours, VPN 512 / 65528–65530 | **NOT drawn** (VPN0 is the underlay's subject; the rest are reserved) |
| The real IPsec+BFD tunnel-to-tunnel mesh between Edges, OMP routes, service chaining, control policy | **NOT drawn** (out of scope, by design — see above) |

Every synthetic overlay port uses the pseudo label `Dummy N` (on the cloud side
and on the LAN switch side; the Edge side uses its real interface name, or
`Vpn <id>` on the WAN side) and their Speed/Duplex/Port-Type are `Unknown`.

### Overlay: WAN side up, LAN side down

Each Edge is drawn with its **VPN membership above** it and its **real LAN
ports below** it, so a site reads the way it actually works — for `br1-edge1`
in the bundled sample:

```
overlay_wp_        [ Corporate ]      [ PCI ]      [ Guest ]     ← one cloud per VPN
                        │                │             │           (L2 segment HERE only)
br1                 'Vpn 10'        'Vpn 11'      'Vpn 12'       ← SYNTHETIC, VRF, no IP
                        └────────────────┼─────────────┘
                                  [ br1-edge1 ]                  ← tier row 0
                        ┌────────────────┼─────────────┐
              'GigabitEthernet 3'  'Gi 4'         'Gi 5'         ← REAL, real IP + VRF
                10.10.3.1/24     10.11.3.1/24  10.12.3.1/24
                        └────────────────┼─────────────┘
                                  [  Dummy_1  ]                  ← SYNTHETIC, tier row 4
                                 (split by VRF, no IP)
```

> [!IMPORTANT]
> **The `Vpn <id>` ports and the `Dummy_<n>` LAN switches do not exist in the
> vManage data.** They are drawing devices this converter invents:
>
> - A `Vpn <id>` port is **not an interface**. It represents the Edge's
>   *membership* of that Service VPN, which is why it carries a VRF but no IP
>   address. The real overlay data path is the SD-WAN IPsec/OMP fabric riding
>   VPN 0 — drawn in the **underlay** diagram, not here.
> - A `Dummy_<n>` switch is **not a real device**. It is one synthetic
>   aggregation point per Edge, so the Edge's real LAN ports terminate
>   somewhere below it (separated by VRF) instead of floating unconnected.
>   Whatever LAN switching actually exists behind those ports is invisible to
>   vManage.
>
> Both are listed under `SYNTHETIC` in `sdwan_overlay_report.md`.

Splitting the two directions is what makes the real interface data survive: the
physical port keeps its own IP and subnet on the L3 diagram (it is a genuine
routed interface in a VRF), while the "which VPNs is this Edge in?" question is
answered by the links going up to the clouds, without either use fighting the
other for the same port.

### Overlay: VRF names

The L3 instance (VRF) written by `rename l3_instance` is **`<VPN name>_vpn<id>`**
— `Corporate_vpn10`, `PCI_vpn11`, `Guest_vpn12` (format configurable via
`vrf_name_format`). A VPN with no configured name uses plain **`vpn<id>`**
(`vpn12`) rather than the cloud's `VPN <id>` fallback label, which would
produce a redundant `VPN 12_vpn12`. The numeric id is always present so the VRF
stays unambiguous in the L3 interface table even if two VPNs share a name.

The same VRF is written onto every port that belongs to that VPN: the Edge's
`Vpn <id>` port, its physical LAN ports, the facing LAN-switch ports, and its
Loopbacks. The LAN switch is therefore split by VRF internally rather than
reading as one flat LAN.

### Area naming: `site-name` first

Both diagrams name each NS area after the site's **`site-name`** as reported by
`/dataservice/device` (`site-id 3` → `br1`, controllers → `controllers`), which
is far more readable than a bare number. Resolution rules
(`sdwan_topology.build_site_area_map`, shared by both mappers so they can never
disagree):

- the most frequently reported `site-name` across that site-id's devices wins
  (alphabetically first on a tie);
- characters outside `[A-Za-z0-9_.-]` are folded to `_`;
- a site-id whose devices report **no** `site-name` falls back to `site<id>`
  (the behaviour before this feature, so an older export is unaffected);
- if two **different** site-ids resolve to the same name, every one of them is
  disambiguated to `<name>_site<id>` — otherwise two genuinely separate sites
  would be fused into one NS area;
- a device with no `site-id` at all goes to the `default` area.

No extra API call is needed: `site-name` is already part of each raw
`/dataservice/device` record. (The `/dataservice/site*` paths returned HTTP 404
on the vManage this was validated against, and
`/dataservice/template/policy/list/site` both disagreed with `site-name` and
omitted some sites, so neither is used.)

### Overlay: one vpn-id = one cloud

Each Service VPN gets exactly ONE waypoint cloud **fabric-wide**, which every
member Edge's interfaces in that VPN attach to. In reality each site normally
has its own subnet inside a given VPN, so this is a deliberate
**abstraction**: the L2 diagram shows one broadcast domain per VPN (which is
what "everyone in this VRF can reach everyone else" looks like at a glance),
while the L3 diagram still separates the real per-site subnets correctly,
because every port keeps its own reported IP/mask.

### Overlay: why the L2 segment is cloud-side only

Each WAN-side overlay link is deliberately **asymmetric**. Exactly one L2
segment — the VPN label, nothing else — is placed on the **cloud's `Dummy N`
port**. **No** L2 segment is placed anywhere else: not on the Edge's `Vpn <id>`
port, not on its physical Service VPN ports, and not on any LAN-switch port.
The LAN-side links carry no L2 segment at all.

This is not a stylistic choice. In Network Sketcher, **any port that carries
an L2 segment is treated as an L2 switchport and is removed from the L3
interface table.** Putting the VPN segment on any of those ports therefore
makes it unaddressable: `add ip_address_bulk` reports `L3 interface not
found: <device>:<port>` and `rename l3_instance` reports `No matching entry
found for hostname ... and portname ...`, so the port silently ends up with
neither its IP nor its VRF.

The asymmetric arrangement is also the physically honest one. An Edge's
Service VPN interface really *is* a routed L3 interface inside a VRF, so it
belongs in the L3 interface table with its IP and `l3_instance`. The segment
exists purely to group the cloud's internal ports into **one broadcast domain
per VPN** in the L2 diagram, which only requires the cloud side to carry it.

The segment name is the **VPN name** (`Corporate`), a deliberate departure
from the `Vlan<id>` convention the other converters in this repo use: there
is no VLAN in an SD-WAN Service VPN, and Network Sketcher renders a
non-`Vlan`-prefixed segment name on a WayPoint's `Du N` port correctly.

### Loopbacks (both modes)

A Service VPN Loopback becomes an **unconnected** virtual port — `add
virtual_port_bulk`, its IP, and its VRF, with **no** L1 link and **no** L2
segment — so it appears on the L3 diagram only. The underlay draws them
identically, from the same shared plan. This follows Network
Sketcher's own rule set, where RULE 15 ("Mandatory SVI-to-L2 segment binding")
names Loopback as an explicit exception, and matches every other converter in
this repo: none of them has ever made a Loopback an L1-link endpoint.

### Overlay: Service VPN names

VPN names come from vManage's **feature templates**
(`/dataservice/template/feature` → `templateDefinition.vpn-id` /
`.name`), scanned once fabric-wide by `fetch_from_vmanage.py` and stored in
the export's `vpn_names` block. They are therefore a **template setting, not
device running-config**: an untemplated device, or a fabric managed entirely
through Configuration Groups, may resolve no names at all. In that case each
cloud falls back to the `vpn_name_fallback_format` configuration value
(default `VPN {id}`, e.g. `VPN 10`), the VRF falls back to `vpn<id>` (see
"Overlay: VRF names" above), and the count is reported in
`sdwan_overlay_report.md`.

A fabric-wide scan is used deliberately in preference to walking each
device's own attached device-template chain. That chain was **verified to
break** on a live vManage: some devices report a device-template name that
does not exist under `/dataservice/template/device` at all, and
`/dataservice/template/device/config/attached/{id}` returns empty for every
device in a Config-Group-managed fabric. When two templates pin the same
`vpn-id` to different names, the winner is: a user-defined template beats a
`factoryDefault` one, then the higher `attachedMastersCount`, then the
alphabetically first `templateName`.

### Overlay L3 IP addresses

Each Service VPN interface has its reported IPv4 address/mask drawn on the
overlay's L3 diagram (`add ip_address_bulk`) and its VPN's VRF written as that
port's L3 instance (`rename l3_instance`), so the L3 diagram is separated by
VRF exactly as the real fabric is. The mask reported by vManage is used
as-is; a host `/32` is a fallback used only when that mask is missing or
unparseable.

Only the Edge's **own real interfaces** (physical VRF ports and Loopbacks) ever
get an IP. The synthetic `Vpn <id>` ports, the LAN-switch ports and the Service
VPN waypoint clouds carry a VRF at most — none of them is a real Layer-3
endpoint.

---

## ⚠️ Concepts that can NOT be fully represented

> [!IMPORTANT]
> Network Sketcher is a VLAN/port diagram tool; SD-WAN is a colour-keyed
> IPsec+BFD tunnel mesh over an arbitrary transport underlay. Both diagrams
> are a **faithful, deliberately scoped-down approximation, not a 1:1
> model**:
>
> - **Underlay cabling is observed only where vManage's LLDP/CDP neighbor
>   endpoints actually populate it — otherwise it is inferred.** vManage's
>   core device/interface APIs return per-device operational STATE only
>   (which VPN0 port carries which IP/colour); this converter separately
>   attempts `/dataservice/device/lldp/neighbors` and
>   `/dataservice/device/cdp/neighbors` as a preferred OBSERVED source, but
>   that data was unavailable in the only live vManage this converter could
>   reach, so every diagram produced so far falls back to grouping the
>   remaining VPN0 physical interfaces by shared `(colour, subnet)` — an
>   INFERENCE, not an observation (see the report's `INFERRED` entries, and
>   "Underlay L1 links: observed vs. inferred" above).
> - **The IPsec+BFD tunnel mesh is deliberately NOT drawn**, in either
>   diagram. The real SD-WAN fabric is a full/partial mesh of every Edge's
>   Tunnel interface to every other Edge sharing a colour (subject to
>   control-policy); drawing that mesh (Edges² edges) would produce an
>   unreadable diagram and does not reflect a *physical* topology anyway.
>   The ONE exception is the OPTIONAL Full Mesh / Hub-and-Spoke / Partial
>   Mesh label suffix on each underlay transport segment (see "Underlay
>   mesh-shape annotation" above) — a classification derived from observed
>   BFD session state, but it never adds any Edge-to-Edge link to the
>   drawing itself.
> - **One Service VPN cloud is an abstraction, not a broadcast domain.** The
>   overlay draws each VPN as a single cloud every member Edge attaches to;
>   the real fabric gives each site its own subnet inside that VRF and
>   forwards between them with OMP, not with L2 flooding. The L3 diagram
>   keeps the per-site subnets separate and correct; the L2 diagram is the
>   deliberately simplified "who is in this VRF" view. See "Overlay: one
>   vpn-id = one cloud" above.
> - **The overlay's WAN side, and the LAN switch in BOTH diagrams, are
>   synthetic scaffolding.** An Edge's `Vpn <id>` ports (overlay only) and its
>   `Dummy_<n>` LAN switch (both diagrams) are invented by this converter to
>   give the VPN-membership links and the real LAN ports somewhere to attach;
>   neither is an object in vManage, and the real LAN switching below an Edge
>   is whatever the site actually has. The genuine overlay data path — IPsec
>   tunnels and OMP over VPN 0 — is a property of the underlay's transport,
>   not of anything drawn here. See "Overlay: WAN side up, LAN side down" and
>   "Underlay: the Service VPN LAN side" above.
> - **`Transport_vpn0` is a label this converter invents, not one vManage
>   reports.** VPN 0 is the SD-WAN Transport VPN — a routing context that
>   vManage does not name the way it names a customer VRF. The underlay still
>   draws it as an `l3_instance` so the L3 table separates transport from the
>   Service VPNs, using `transport_vpn_name` + `vrf_name_format` to build the
>   label. Read it as "these ports live in VPN 0", not as a VRF name you can
>   grep for in a device config.
> - **Service VPN names are template metadata.** They are read from
>   vManage's feature templates, not from device running-config, so a
>   Config-Group-managed or untemplated fabric falls back to `VPN <id>`
>   labels and `vpn<id>` VRFs — see "Overlay: Service VPN names" above.
> - **No OMP routes, service chaining, or control policy.** What each
>   Service VPN actually advertises, and any policy that restricts
>   VPN-to-VPN or site-to-site reachability inside it, is not modelled: the
>   overlay shows VRF *membership*, not the resulting reachability matrix.
> - **The controllers' path to the transport is a synthesised abstraction,
>   not an observed one.** Control traffic is NOT out-of-band: it rides the
>   VPN 0 transport this underlay draws, and vManage reports it per
>   (Edge, controller, colour) triple. What vManage does *not* report is the
>   routed distance between a controller's own subnet and each transport
>   subnet — `ip/routetable` answers empty — so the `Dummy_l3` router and
>   `Dummy_l2` switch that carry the controllers stand in for hops of unknown
>   length rather than modelling real devices. The single uplink is drawn from
>   real evidence (observed control connections resolved to that cloud by
>   address, then ranked), but *which* cloud carries the control plane is a
>   best guess, not a measurement, and the two hops themselves are invented.
>   Per-controller colour nuance is
>   annotated rather than drawn. Controllers remain entirely absent from the
>   overlay (they are not a Service VPN endpoint). vManage's VPN 512 port is
>   the genuine out-of-band management network and is excluded from both
>   diagrams.
> - **`device-model` personality inference is best-effort.** vManage's own
>   `device-model` naming is not fully standardised across releases/platforms
>   (`vedge-cloud`, `vedge-C8000V`, `c8000v`, `vedge-1000`, ...); when the
>   `personality` field itself is present (the common case), it is used
>   directly and this fallback never triggers.
>
> **Always validate against vManage before relying on the diagram.**

## Device color conventions

The generated `rename attribute_bulk` command writes a coloured cell into the
**Default** column of the Network Sketcher Attribute sheet —
`"['DEVICE',[R,G,B]]"` (WayPoints keep their token: `"['WayPoint',[R,G,B]]"`)
— so every device is colour-coded by role in the Device Table. The base
palette is **shared across every converter in this repo**:

| Colour | RGB | Meaning | Used in sdwan_converter |
|--------|-----|---------|------------------------|
| 🟥 Light red | `[255, 204, 204]` | **Server-role endpoint** | ✅ underlay: vManage / vSmart / vBond (control-plane appliances) |
| 🟩 Light green | `[235, 241, 222]` | **Observed network gear** — real router | ✅ underlay: every SD-WAN Edge |
| 🟪 Light purple | `[221, 204, 255]` | **Logical / overlay** — a real participant in the logical topology | ✅ overlay: the Edges and the Service VPN waypoint clouds |
| ⬜ Light gray | `[200, 200, 200]` | **Inferred / synthesised** — placeholders that do not exist in the source | ✅ underlay: the inferred Transport-`<colour>`-`<network>` segment clouds, the `Dummy_<n>` LAN switches **and** the controllers' `Dummy_l3` / `Dummy_l2` hops • overlay: the `Dummy_<n>` LAN switches |

Two further fixed cell colours appear in every device row (set by the shared
`ns_command_builder`, not role-based): the **Model** column is pink
`[255, 183, 219]` and the **OS** column is light blue `[200, 230, 255]`.

**In sdwan_converter:** the **underlay** colours controllers red and Edges
green, with the inferred transport-segment clouds, the synthetic `Dummy_<n>`
LAN switches and the controllers' `Dummy_l3` / `Dummy_l2` hops gray — the same
"synthesised placeholder" meaning in each case (nothing in the underlay is
purple — there is no
logical/overlay concept there; the LAN switches are gray in both diagrams
because they are the same synthesised placeholder in both). The
**overlay** colours its Edges and Service VPN clouds light purple, since both
are participants in the same logical VRF topology, but colours each synthetic
`Dummy_<n>` LAN switch **gray** instead: unlike the Edges (real devices) and
the VPN clouds (a real Service VPN's abstraction), a LAN switch is a pure
placeholder invented by this converter with no counterpart of any kind in
vManage — exactly what the gray palette entry means, and the same colour the
underlay's inferred transport clouds already use.

### Known Network Sketcher behaviour: the L3 diagram tints device labels

The colours above are what the **L1/L2 diagrams and the Device Table** show. In
the **L3 diagram** the same devices come out slightly purple, and this is the NS
L3 renderer's own behaviour, **not** something this converter emits.

Measured on the generated underlay SVGs, NS adds a fixed `+(15, 10, 25)` RGB
offset to the name cell of every device that carries L3 content:

| Device | Requested (Default cell) | L1/L2 + Device Table | L3 diagram |
|---|---|---|---|
| SD-WAN Edge | `[235, 241, 222]` | `rgb(235,241,222)` | `rgb(250,251,247)` |
| `Dummy_<n>` LAN switch | `[200, 200, 200]` | `rgb(200,200,200)` | `rgb(215,210,225)` |
| Controller (vManage / vSmart / vBond) | `[255, 204, 204]` | `rgb(255,204,204)` | `rgb(255,214,229)` (red clamped at 255) |
| `Dummy_l3` / `Dummy_l2` (no L3 content) | `[200, 200, 200]` | `rgb(200,200,200)` | `rgb(200,200,200)` |

The VRF blocks themselves are a separate fixed colour (`rgb(230,224,236)`) that
no converter controls either.

Because the two synthetic control-plane hops carry no IP, they stay true gray in
the L3 diagram. The **controllers** do carry one now (see "Underlay: the
controllers' control-plane path"), which is why their red picks up the same
shift there. Cancelling the offset would mean requesting a
pre-compensated colour (roughly `185, 190, 175` to land on gray) — which would
make the L1/L2 diagrams and the Device Table greenish-gray instead, so it is
deliberately **not** done. Fixing it belongs upstream in
[network-sketcher](https://github.com/cisco-open/network-sketcher).

## Accuracy caveats

- **Underlay L1 links are OBSERVED (via LLDP/CDP) only where vManage's
  neighbor endpoints actually populate them — INFERRED otherwise.** This
  converter attempts `/dataservice/device/lldp/neighbors` and
  `/dataservice/device/cdp/neighbors` as a preferred, real device-to-device
  source before falling back to grouping the remaining VPN0 physical
  interfaces by shared **transport colour** into one gray cloud. **This has
  been validated end-to-end only in the inference-fallback
  path** — the one live vManage this converter has been tested against does
  not expose LLDP/CDP neighbor data at all, so the OBSERVED path is
  implemented against a best-effort field-name convention that has not been
  confirmed against a real physical vManage's actual API response yet;
  verify it there before fully trusting it. A colour with only one member
  Edge still gets its own single-member cloud (representing that Edge's
  dedicated WAN access circuit) rather than being dropped, and a colour whose
  members sit on several access subnets becomes ONE cloud carrying all of
  them — the subnet is not part of the identity.
- **The underlay's L2 segment is asymmetric — cloud side only — and named
  after the transport colour.** It is written onto every `Dummy N` port of the
  inferred transport cloud and onto NO Edge port, because Network Sketcher
  drops any L2-segmented port from the L3 interface table (which would strip
  the Edge port's IP address). Without the segment the Underlay L3 diagram is
  empty; with it, each transport renders as one broadcast domain. There is
  exactly one cloud per colour, so the colour is an unambiguous segment name.
  See "Underlay: why the L2 segment is cloud-side only" above.
- **The `Dummy 0` SVI on each WayPoint cloud is SYNTHESISED and carries no
  IP.** Both modes add one per cloud, self-bound to that cloud's existing L2
  segment (the transport colour in the underlay, the VPN label in the
  overlay), with no L1 link. It has no vManage counterpart whatsoever — it is
  an L3/SVI anchor for the segment, not an interface — and it never collides
  with the cloud's L1 `Dummy N` ports, which are numbered from 1. See "Both
  modes: every WayPoint cloud carries a `Dummy 0` SVI" above.
- **An underlay area with no L3 interface is emitted last in the area row.**
  This works around an NS engine bug (`calculate_area_offset` raises
  `KeyError` for an L3-less area that is not last). It changes the
  left-to-right order of the site areas only; the L1 drawing is unaffected. A
  row that is L3-less end to end (the `wan_wp_` waypoint row) is left alone.
  See "Underlay: areas with no L3 interface sort last" above.
- **The `controllers` area is lifted to the TOP row of `add area_location`,
  and reversed internally.** Pure layout, so the control-plane path points down
  into the transport clouds; driven by device personality, not by a hardcoded
  area name. See "Underlay: the controllers' area is the top row" above.
- **The underlay also draws the Service VPN LAN side, and its `Dummy_<n>` LAN
  switches are SYNTHETIC.** Below each Edge the underlay draws the same LAN
  side as the overlay — the Edge's real Service VPN physical interfaces
  (with their reported IPs) linked down to one invented per-Edge `Dummy_<n>`
  switch, and its Service VPN Loopbacks as unconnected virtual ports — with
  the VPN's VRF on all of them. The switch has no vManage counterpart (the
  real LAN switching behind those ports is invisible to vManage), which is
  why it is drawn **gray** in both diagrams. Both mappers plan this side with
  the same shared helper (`src/sdwan_lan_side.py`), so the `Dummy_<n>` names
  and the Edge→switch assignment are identical in the two command files by
  construction rather than by coincidence. See "Underlay: the Service VPN LAN
  side" above.
- **The VPN 0 transport interfaces are drawn in a `Transport_vpn0` VRF, and
  its NAME is a modelling choice.** `GigabitEthernet 1` / `GigabitEthernet 2`
  (and every other VPN0 physical port) keep their IP and their L1 link to the
  transport cloud and additionally get `rename l3_instance` with VPN 0's own
  L3 instance, so the L3 table separates transport from the Service VPNs. The
  label is built from vpn-id 0 and the `transport_vpn_name` configuration
  value (default `Transport`) rather than from vManage's own VPN 0 template
  name, which is typically a placeholder — so `Transport_vpn0` is this
  converter's label for the Transport VPN, not a string vManage reports.
  The VRF is written on the **Edge side only**: the inferred transport clouds
  carry the colour L2 segment and no L3 instance at all, exactly as the
  overlay's waypoint clouds do, and the **controllers** get none either — they
  are `Server`-role appliances, not routing nodes, so their VPN 0 ports carry an
  IP and stay in Network Sketcher's default L3 instance.
- **Overlay waypoints are an abstraction, not cabling.** The per-VPN cloud
  does not exist as such in vManage — it represents every Edge's membership
  of that Service VPN, drawn as one shared segment even though the real
  fabric routes between per-site subnets with OMP. Overlay link ports always
  use the pseudo label `Dummy N` on the cloud side; every synthesised element
  is listed under `SYNTHETIC` / `MODEL` / `INFERRED` in
  `sdwan_overlay_report.md`.
- **The overlay's `Vpn <id>` ports and `Dummy_<n>` LAN switches are
  SYNTHETIC** (the LAN switches likewise in the underlay, see above). Neither
  exists in the vManage data. A `Vpn <id>` port stands
  for the Edge's *membership* of that Service VPN (hence a VRF but no IP), not
  for an interface — the real overlay data path is the SD-WAN IPsec/OMP fabric
  in VPN 0, which is drawn in the **underlay**. A `Dummy_<n>` switch is a
  per-Edge aggregation point invented so the Edge's real LAN ports terminate
  somewhere below it rather than floating unconnected; the LAN switching that
  actually exists behind them is invisible to vManage. The LAN switches are
  drawn **gray** for exactly that reason, while the Edges and the Service VPN
  clouds stay purple. See "Overlay: WAN side up, LAN side down" and "Device
  color conventions" above.
- **The overlay's L2 segment is asymmetric — cloud side only.** The VPN
  label is written onto the waypoint cloud's `Dummy N` port and onto NO other
  port (not the Edge's `Vpn <id>` port, not its physical Service VPN ports,
  not the LAN switch's ports), because Network Sketcher treats any port
  carrying an L2 segment as an L2 switchport and drops it from the L3
  interface table — which would make that port's `add ip_address_bulk` and
  `rename l3_instance` fail outright. Keeping those ports segment-free
  leaves them routed L3 ports (what the Edge's Service VPN interfaces really
  are) while the cloud-side segment still renders one broadcast domain per VPN
  in the L2 diagram. See "Overlay: why the L2 segment is cloud-side only"
  above.
- **Area names come from `site-name`, with a `site<id>` fallback.** An export
  whose device records carry no `site-name` (e.g. a hand-built one, or the
  `_with_bfd` bundled sample) keeps the previous `site<id>` area names; two
  different site-ids reporting the same name are disambiguated to
  `<name>_site<id>` rather than merged. See "Area naming: `site-name` first"
  above.
- **Service VPN names are best-effort template metadata**, not device
  running-config, and fall back to `VPN <id>` — see "Overlay: Service VPN
  names" above.
- **Service VPN Loopbacks are intentionally unconnected** (L3 diagram only),
  per Network Sketcher RULE 15's Loopback exception — see "Overlay:
  Loopbacks" above.
- **An Edge with no Service VPN interface is absent from the overlay
  entirely** (rather than drawn as an isolated node); it is still present in
  the underlay. The count is reported in `sdwan_overlay_report.md`.
- **Unresolved transport colours** (a VPN0 interface whose IP could not be
  matched against `control/waninterface`) are grouped under the
  `unknown_color_label` configuration value (default `"unknown"`) rather than
  dropped, and counted in both reports.
- **`personality` inference from `device-model`** only triggers when the
  `personality` field itself is absent from the export; it is a best-effort
  keyword match (lower confidence, flagged in `sdwan_inventory.csv`).
- **Mesh-shape classification (`Full Mesh`/`Hub-and-Spoke`/`Partial Mesh`)
  requires OPTIONAL `bfd_sessions` data and is an underlay label suffix
  only** — see "Underlay mesh-shape annotation" above. Its field names
  (`color`, `system-ip`, `site-id`, `state`) WERE verified against a live
  vManage (unlike the LLDP/CDP neighbor field names), so no field-name caveat
  applies here. Two real caveats do: same-site Edge pairs are excluded from
  the "should be connected" check (based on this converter's live validation
  environment's observed behaviour), and the classification reflects the
  OBSERVED live fabric rather than policy intent — a down session makes the
  fabric look sparser than it was designed to be.
- **Overlay L3 IPs use each interface's OWN reported mask, not a forced
  `/32`.** A `/32` is used only when that mask is missing/unparseable in the
  export — see "Overlay L3 IP addresses" above.
- **WayPoint left-to-right ordering is an ESTIMATE, not a guaranteed
  pixel-optimal layout.** Both diagrams order same-row WayPoint clouds (e.g.
  multiple transport-colour / Service VPN clouds) to
  minimise a placement-time estimated total wire length (weighted by
  L1-link count to each linked site device's column position) — real
  on-canvas pixel length is only known once Network Sketcher actually
  renders the diagram.

## `fetch_from_vmanage.py` — pulling the model over the REST API

The converter reads local files, but `fetch_from_vmanage.py` grabs the model
straight from a reachable vManage and writes a `convert`-ready combined JSON.
Retrieval is **read-only** (GET only, plus the one unavoidable login POST) —
it never modifies the SD-WAN fabric. It authenticates with vManage's
form-based login (`POST /j_security_check` with `j_username`/`j_password`,
session cookie), then fetches the CSRF token required on every subsequent
request (`GET /dataservice/client/token`, sent back as the `X-XSRF-TOKEN`
header). Credentials come from a CLI arg or the `VMANAGE_PASSWORD` env var
(never hard-coded, never logged); vManage's typically self-signed lab
certificate is accepted by default (`--verify-tls` to enforce verification).

It then pulls the device inventory (`/dataservice/device`) and, per device
(keyed by `system-ip`), its interface state
(`/dataservice/device/interface?deviceId=<system-ip>`), WAN
transport-colour state
(`/dataservice/device/control/waninterface?deviceId=<system-ip>`), and —
best-effort, the **preferred** source for the underlay's L1 links when it
works — observed neighbor adjacency
(`/dataservice/device/lldp/neighbors?deviceId=<system-ip>` and
`/dataservice/device/cdp/neighbors?deviceId=<system-ip>`), plus live BFD
tunnel-session state (`/dataservice/device/bfd/sessions?deviceId=<system-ip>`)
— the source the underlay's mesh-shape annotation uses (see "Underlay
mesh-shape annotation" above) — note all five endpoints require the
**`system-ip`**, not the device `uuid`, in `deviceId`.

Finally, ONE fabric-wide (not per-device) pass resolves each Service VPN's
name for the overlay's clouds: `GET /dataservice/template/feature` lists
every feature template, each `templateType` of `cisco_vpn` (IOS-XE/cEdge) or
`vpn-vedge` (Viptela-OS/vEdge) is fetched with `GET
/dataservice/template/feature/object/{templateId}`, and a
`{vpn-id, name}` pair is accepted only when BOTH are `vipType: "constant"`
(a `variableName` means the template does not pin the value fabric-wide). See
"Overlay: Service VPN names" above for the conflict-resolution order and why
a per-device template chain is deliberately not used.

Each per-device query is independent and a failed one is **skipped, not
fatal** — a partial fetch still produces the best diagram the data allows.
This matters most for the LLDP/CDP neighbor calls: they are not guaranteed
to exist on every vManage release/platform (see "Underlay L1 links: observed
vs. inferred" above), so a 404/empty response from either is treated exactly
like any other optional endpoint here. (The BFD sessions endpoint has been
verified to exist and return structured data on this converter's live
validation vManage, unlike LLDP/CDP.)

### Combined JSON format (also the offline input)

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

Each `<device>` is a raw `/dataservice/device` record (at minimum
`host-name`, `system-ip`, `site-id`, `device-model`, `personality`, `uuid`,
`reachability`), normally also including `site-name` — the human-readable site
label both diagrams use as the NS area name (see "Area naming: `site-name`
first" above). Each `<interface>` is a raw `/dataservice/device/interface`
record (`ifname`, `vpn-id`, `ip-address`, `ipv4-subnet-mask`,
`if-admin-status`). Note that a real vManage does **not** return an `af-type`
field on these records at all (only the bundled synthetic sample carries one);
nothing in this converter filters on it. Service VPN membership comes solely
from each interface's `vpn-id` — `/dataservice/device/vpn` was observed to
always return an empty `"data": []` array and is therefore not used. Each
`<waninterface>` is a raw
`/dataservice/device/control/waninterface` record (`color`, `private-ip`,
`public-ip`). Each `<bfd-session>` is a raw
`/dataservice/device/bfd/sessions` record (`color`, `system-ip` of the
REMOTE peer, `site-id` of the remote peer, `state`) — see "Underlay
mesh-shape annotation" above; unlike the neighbor records below, this
endpoint's field names WERE verified against a live vManage.

`vpn_names` maps a Service VPN id to its configured name. Keys are always
**strings** on read, even though vManage's template API reports the id as an
integer while its interface API reports the same id as a string.

`lldp_neighbors` / `cdp_neighbors` / `bfd_sessions` / `vpn_names` are all
**optional** — `bfd_sessions` is absent from the base bundled sample and
`vpn_names` from the `_with_bfd` one. `lldp_neighbors` / `cdp_neighbors` are
also absent on any export from a platform whose vManage does not expose the
endpoint (see above). Each `<lldp-neighbor>` / `<cdp-neighbor>` is
whatever raw record `/dataservice/device/lldp/neighbors` /
`/dataservice/device/cdp/neighbors` returns; **the exact field names are
UNVERIFIED against a real physical vManage**, so the parser tries several
plausible key names per field (see the accuracy caveats above). For
illustration only — this is a synthetic shape, not a captured real
response — a populated entry might look like:

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

`convert.py` also accepts a **directory** of per-endpoint `*.json` dumps
(filenames hint the collection, e.g. `devices.json`, `interfaces.json`).

## Running the output in Network Sketcher

Each `*.txt` is a plain-text script (one command per line; `#` lines are
phase comments) already ordered Phase 1→6. Install
[Network Sketcher](https://github.com/cisco-open/network-sketcher), create an
empty master, run the command lines against it, and export the diagram.

### AI-agent runbook — generate the diagrams without mistakes

> [!IMPORTANT]
> The diagram build runs in the **Network Sketcher engine** (e.g. the
> `network-sketcher` MCP), NOT in this converter — the converter only emits
> the `*.txt` command scripts. The engine names **every** artifact after its
> master (always `[MASTER]no_data.nsm`), so generating a second mode in a
> workspace that still holds the first mode's diagrams makes the build
> **reuse the stale diagrams** (the device table refreshes, but the SVG/HTML
> do not). Follow the rules below to avoid it.

**Rule 1 — one mode = one clean, dedicated workspace.** Never reuse a
workspace across modes or runs. The workspace must be under your **home
directory** (the engine rejects paths outside it).

**Rule 2 — ordered steps, per mode** (`underlay`, then `overlay`):
1. Use a **fresh empty workspace directory for this mode** (e.g.
   `~/ns_ws_underlay`, `~/ns_ws_overlay`). If reusing a directory, first
   delete every prior artifact in it: `[MASTER]*.nsm`, `[L1_DIAGRAM]*`,
   `[L2_DIAGRAM]*`, `[L3_DIAGRAM]*`, `[L1L2L3_DIAGRAM]*`, `[DEVICE_TABLE]*`,
   `[AI_Context]*`.
2. `create_empty_master` → `[MASTER]no_data.nsm`.
3. `run_commands` with this mode's `*.txt`. Pass the commands as a
   **newline-delimited single string with the `#` comment lines removed** —
   NOT a JSON array (an array is parsed as one bad verb and fails). Expect
   `N OK, 0 FAIL`; if any command FAILs, stop and fix before continuing.
4. `build_default_outputs` → expect `Summary: 6/6 succeeded.`
5. **Immediately move/rename** the six artifacts out of the workspace,
   tagged by mode (e.g. `..._underlay.svg` / `..._overlay.html`), before
   touching the next mode.

**Rule 3 — verify before trusting the result (do all three):**
- **Content marker:** the `underlay` L2 SVG must contain a controller name,
  the `Dummy_l3` / `Dummy_l2` hops that carry it, and a transport cloud named
  after a TLOC colour (`biz-internet`, `mpls` — optionally suffixed
  `(Full Mesh)` / `(Hub-and-Spoke)` / `(Partial Mesh)`); the `overlay` L2 SVG
  must contain a Service VPN cloud name (e.g. `Corporate`, or `VPN 10` when
  unresolved) and none of those. A mode whose diagram shows the *other*
  mode's names was built from a dirty workspace. The `Dummy_<n>` LAN
  switches are **not** a usable marker — both modes draw the same ones, by
  design. Note the controllers now reach the L2 SVG through their
  `dummy_l2` segment rather than as isolated nodes; if
  `enable_controller_control_plane_links` is `false` they are isolated again,
  and only the transport-colour cloud remains a marker.
- **Cross-mode diff:** the `underlay` and `overlay` `[L1L2L3_DIAGRAM]` HTML
  files must **not** be byte-identical.
- **Freshness:** each artifact's modification time must be newer than the
  moment you started this mode's build.

If any check fails, the workspace was dirty — redo this mode in a brand-new
empty workspace (Rule 1).

## Configuration (`sdwan_to_ns_config.json`)

Every key is `{value, description, sample}`; only `value` is read. Key
options: `device_naming` (`hostname` | `hostname_ip`, for exports with
duplicate/blank hostnames), `unknown_color_label` (label for a VPN0
interface whose transport colour could not be resolved, default
`"unknown"`), `enable_observed_l1_links` (underlay; default `true` — set to
`false` to skip the LLDP/CDP observed-link pass entirely and force pure
`(colour, subnet)` inference, unconditionally), `prefer_lldp_over_cdp`
(underlay; default `true` — when both protocols report the same local port,
which one's data wins the observed link), `excluded_service_vpns` (**both
modes**; default `[512, 65528, 65529, 65530]`, ints or strings both accepted —
extra Service VPN ids to leave out of both diagrams; **VPN 0 is always
excluded from the Service VPN side unconditionally**, it is drawn as the
underlay's transport instead), `transport_vpn_name` (underlay; default
`"Transport"` — VPN 0's display name inside the transport VRF label, fed
through `vrf_name_format` to give `Transport_vpn0`; blank it for a bare
`vpn0`), `vpn_name_fallback_format` (overlay; default
`"VPN {id}"` — the cloud/segment label for a Service VPN whose configured name
could not be resolved), `vrf_name_format` (**both modes**; default
`"{name}_vpn{id}"` — the `rename l3_instance` VRF name; a VPN with no
configured name always uses plain `vpn<id>` instead), and
`lan_dummy_name_format` (**both modes**; default `"Dummy_{n}"` — the name of
each synthetic per-Edge LAN switch, `{n}` being a global 1-based counter in
sorted Edge-name order; both mappers derive it from the same shared plan, so
the two diagrams always agree on which switch belongs to which Edge).

Three further keys are underlay-only and govern the controllers' control-plane
path: `enable_controller_control_plane_links` (default `true` — set to `false`
to draw the controllers as unconnected inventory nodes, the pre-feature
behaviour), `control_l3_dummy_name` (default `"Dummy_l3"`) and
`control_l2_dummy_name` (default `"Dummy_l2"`).

## Author

Yusuke Ogawa - Architect, Cisco | CCIE#17583

## License

This tool is part of the **Network Sketcher Cisco Extension** project, licensed
under the [Apache License 2.0](../LICENSE). See the [NOTICE](../NOTICE) file for
copyright and third-party attributions.
