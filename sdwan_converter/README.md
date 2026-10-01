# sdwan_converter — Cisco Catalyst SD-WAN (vManage) to Network Sketcher Command Converter

> Validated end-to-end against a live **CML-simulated vManage lab** and the
> Network Sketcher engine: both modes import cleanly and every L1/L2/L3 diagram
> artifact generates correctly. Two scope limits are worth knowing before you
> read a diagram: no other fabric size, release or platform has exercised the
> converter yet, and the OBSERVED (LLDP/CDP) path for the underlay's L1 links is
> implemented against a best-effort, **unconfirmed field-name convention** —
> that lab's vManage returns HTTP 404 for both neighbor endpoints, so every
> diagram produced so far took the INFERRED fallback (see `DESIGN.md` sections
> 3.1 / 5.4).

Convert a [Cisco Catalyst SD-WAN](https://www.cisco.com/site/us/en/products/networking/sd-wan/index.html)
(vManage-managed) fabric into ready-to-run
[Network Sketcher](https://github.com/cisco-open/network-sketcher) command
scripts. The model is pulled from vManage over its **read-only REST API**
(`fetch_from_vmanage.py`) — or from a previously downloaded export — and
converted **entirely offline** into two diagrams.

<img alt="image" src="https://github.com/user-attachments/assets/042c7bbe-1b17-4431-bd80-157195c78cc3" />

<img alt="image" src="https://github.com/user-attachments/assets/8655106d-08b8-46ed-aac7-7e6199642eef" />

[Sample_sd-wan.zip](https://github.com/user-attachments/files/30772635/Sample_sd-wan.zip)

---

## Why two diagrams: underlay vs. overlay

**SD-WAN deliberately decouples the WAN transport that carries the fabric
(VPN 0) from the customer traffic the fabric carries (the Service VPNs / VRFs
— VPN 10, 11, 12, ...).** The transport side is about *cables and circuits*;
the Service VPN side is about *who can talk to whom* — an independent routing
domain OMP advertises fabric-wide, indifferent to which physical circuit it
rides. They answer different questions and do **not** line up 1:1, so this
tool produces **two independent diagrams** (selectable with `--mode`):

| | **Underlay** (`--mode underlay`) | **Overlay** (`--mode overlay`) |
|---|---|---|
| Question it answers | *Which physical ports does each Edge have, and how do they reach the WAN transport(s) above and the LAN below?* | *Which Service VPNs (VRFs) does each Edge participate in, on which ports and subnets?* |
| Contents | vManage/vSmart/vBond + every Edge, with transport-circuit clouds **above** and a per-Edge LAN switch **below** — the **physical** picture, both sides | Every Edge with ≥1 Service VPN interface, with one waypoint cloud per Service VPN **above** and the same per-Edge LAN switch **below** — the Service VPN **abstraction** |
| Controllers | ✅ drawn and connected, top area row (see `DESIGN.md` §3.7) | ❌ not drawn (no Service VPN interface) |
| VPN 0 transport ports | ✅ drawn, with IP + `Transport_vpn0` VRF | ❌ not drawn |
| Service VPN cloud abstraction | ❌ not drawn | ✅ drawn |
| Output | `*_sdwan_underlay.txt` | `*_sdwan_overlay.txt` |

They deliberately share the exact same LAN side (`DESIGN.md` §3.5), so the two
diagrams line up device for device.

> **What vManage's core device/interface APIs do NOT provide** — unlike
> NDFC/Catalyst Center (real observed cabling) and like ACI (config-only, no
> cabling), vManage's device/interface APIs return per-device operational
> STATE only, with no adjacency between two Edges' WAN ports built in. This
> converter also attempts vManage's LLDP/CDP neighbor endpoints as a
> best-effort **OBSERVED** source for the underlay's L1 links; when absent (as
> in every export validated so far), the underlay's transport circuits fall
> back to being **INFERRED** (grouped by shared transport colour) — see
> `DESIGN.md` section 3.1.

**Explicitly out of scope for BOTH diagrams (by design):** the real
IPsec+BFD tunnel-to-tunnel mesh between Edges (never drawn as links), VPN0
**Tunnel** interfaces, and OMP-advertised routes / centralized control
policy. Observed live BFD *session state* is the one partial exception: when
present, it only classifies each **underlay** transport segment's fabric mesh
shape (Full Mesh / Hub-and-Spoke / Partial Mesh — `DESIGN.md` §3.3); it never
adds Edge-to-Edge links.

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
like (different `--mode` / config) with no further vManage access. A bundled
sample (`Input_data/sample_sdwan_export.json`) lets you try step 3
immediately without any vManage at all — it is a **sanitized real vManage
export** (12 devices: 1 vManage, 2 vSmart, 1 vBond, 8 Edges across 5 sites —
one 2-Edge branch, two single-Edge branches and two 2-Edge datacenters — on
two TLOC colours, `biz-internet` and `mpls`); see `tools/sanitize_vmanage_export.py`
for exactly which fields were replaced (device `uuid`/`board-serial`/
geo-coordinates, **every** timestamp — epoch-ms and ISO-8601 alike, including
the ones embedded inside `vdevice-dataKey`, so the export's real capture window
is not recoverable — and the `_meta.host` URL only; every IP address, VPN/VRF,
BFD session and control-connection value other than its timestamps is untouched
real fabric data). It carries `site-name`, `vpn_names`, a full set of Service VPN
interfaces (Guest VPN on the branch Edges only) and an observed
`bfd_sessions` block, so one conversion run also shows the underlay's
mesh-shape annotation (both `biz-internet` and `mpls` render as
`(Full Mesh)`). One branch site's two Edges each also have one VPN0 physical
interface whose TLOC colour vManage did not resolve — see "Unresolved
transport colours" below and `DESIGN.md` section 3.2 for how those are drawn
as their own single-member `unknown_N` clouds rather than being merged
together.

## Modes (`--mode`, default `both`)

| `--mode` | What it draws | Built from |
|----------|---------------|-----------|
| `underlay` | vManage/vSmart/vBond + every Edge, plus its VPN0 **physical** interfaces' L1 links — OBSERVED (LLDP/CDP) where available, else INFERRED gray clouds named after the transport colour, one per TLOC colour (`DESIGN.md` §3.1–§3.2). Each cloud carries a colour-named L2 segment on the cloud side only (§3.4) plus a synthesised `Dummy 0` SVI. Controllers sit in the TOP area row, connected through two synthetic hops down to the transport cloud their control connections most point at (§3.7). **Below** each Edge the underlay also draws its real Service VPN LAN side (§3.5), while the VPN0 transport ports above carry the `Transport_vpn0` VRF (§3.6) | `/dataservice/device`, `/dataservice/device/interface`, `/dataservice/device/control/waninterface`, `/dataservice/device/lldp/neighbors`, `/dataservice/device/cdp/neighbors`, plus (optional) `/dataservice/device/bfd/sessions`, `/dataservice/device/control/synced/connections` (or live `/connections`) and `/dataservice/device/control/connectionshistory` |
| `overlay` | Every SD-WAN Edge with ≥1 **Service VPN** (VRF) interface, split into a WAN side (a synthetic `Vpn <id>` port per VPN, up to ONE purple waypoint cloud per VPN — `DESIGN.md` §4.1) and a LAN side (real physical VRF interfaces down to that Edge's `Dummy_<n>` switch). Every port on both sides gets the VPN's VRF (`Corporate_vpn10`) as its L3 instance. Service VPN **Loopbacks** are unconnected virtual ports (IP + VRF only) | `/dataservice/device`, `/dataservice/device/interface` filtered to non-reserved `vpn-id`, plus (optional) `/dataservice/template/feature` for the VPN names |
| `both` | `underlay` + `overlay` (default) | — |

## Output files

| File | Mode | Description |
|------|------|-------------|
| `<stem>_sdwan_underlay.txt` | underlay | NS CLI commands (Phase 1–6) for the physical/circuit view, incl. the cloud-side L2 segments, every Edge port's IP, and the Service VPN LAN side below each Edge |
| `<stem>_sdwan_overlay.txt` | overlay | NS CLI commands (Phase 1–6) for the Service VPN overlay |
| `ns_model_underlay.json` / `ns_model_overlay.json` | each | Intermediate topology model (debug) |
| `sdwan_inventory.csv` | underlay | Device inventory + stencil mapping (audit) |
| `sdwan_underlay_report.md` / `sdwan_overlay_report.md` | each | Counts + accuracy caveats (incl. every inferred/synthesised element) |

---

## Overlay/Underlay representation rules

The essentials for reading a diagram correctly; the full rationale for each is
in `DESIGN.md`.

- **VPN0 vs. Service VPN classification** decides which diagram(s) an
  interface appears in at all (`DESIGN.md` §2.2): VPN0 physical ports →
  underlay only; Service VPN physical ports and Loopbacks → both; Tunnel`<N>`
  and reserved VPNs (512, 65528–65530) → neither.
- **Areas are named after `site-name`** (`br1`), falling back to `site<id>`
  (§2.3). Both diagrams use identical area names.
- **Every WayPoint cloud carries a synthesised, IP-less `Dummy 0` SVI**
  self-bound to that cloud's own L2 segment, so the L2/L3 diagrams render one
  broadcast domain per cloud (§2.4).
- **Every L2 segment is cloud-side only** (never on the Edge/LAN-switch side):
  Network Sketcher drops any L2-segmented port from the L3 interface table, so
  segmenting the Edge side would silently strip its IP (§3.4, §4.4).
- **Underlay: one transport cloud per TLOC colour, star topology, never a
  mesh.** A colour-**unresolved** interface is the one exception — it is
  NEVER merged with another unresolved one (see "Unresolved transport
  colours" below and §3.2).
- **Underlay: every Edge port carries a VRF** — `Transport_vpn0` above
  (VPN0 physical ports, §3.6), the Service VPN's VRF below (§3.5) — mirroring
  the overlay, where the Service VPN VRF (`Corporate_vpn10`) is on every port
  (§4.2).
- **Underlay: the controllers are connected**, hung off two synthetic hops
  down to the ONE transport cloud their control connections most point at
  (§3.7); a controller's own VPN0 port carries its reported IP where one
  resolves, with no VRF (it is a `Server`, not a routing node).
- **Loopbacks are unconnected virtual ports** (IP + VRF, no L1/L2) in both
  diagrams (§2.5).
- **Overlay: one Service VPN = one waypoint cloud fabric-wide** — an
  abstraction; the L3 diagram still separates real per-site subnets
  correctly even though the L2 diagram shows one broadcast domain per VPN
  (§4.3).

### Unresolved transport colours

A VPN0 interface whose IP could not be matched against `control/waninterface`
is NOT dropped, but it is also NEVER merged with another such interface:
`unknown_color_label` (default `"unknown"`) is a placeholder meaning "could
not be identified", not a real shared TLOC colour, so there is no evidence
two such interfaces ride the same WAN. Each gets its OWN single-member
underlay cloud instead, named `<unknown_color_label>_1`, `_2`, ... in
deterministic `(device, port)` order — e.g. two different Edges' unresolved
ports land in `unknown_1` and `unknown_2`, never in one shared `unknown`
cloud. Each cloud's `colour=` attribute still reports the plain
`unknown_color_label` value; only the cloud's NAME carries the disambiguating
number. Counted in both reports as `unresolved_color` (interfaces) and
`unresolved_color_segments` (clouds created — always equal by construction).
See `DESIGN.md` section 3.2 for the two-pass algorithm and worked example.

---

## ⚠️ Concepts that can NOT be fully represented

> Network Sketcher is a VLAN/port diagram tool; SD-WAN is a colour-keyed
> IPsec+BFD tunnel mesh over an arbitrary transport underlay. Both diagrams
> are a faithful, deliberately scoped-down approximation — see `DESIGN.md`
> section 5.7 for the full rationale behind each row.

| Concept | Why it can't be fully drawn |
|---|---|
| Underlay cabling | OBSERVED only where LLDP/CDP populate it (unconfirmed schema, §5.4); INFERRED (by TLOC colour) otherwise |
| IPsec+BFD tunnel mesh | Deliberately NOT drawn as links (Edges² edges is unreadable and not physical topology); only a Full/Hub-Spoke/Partial Mesh label suffix, from observed BFD state (§3.3) |
| One Service VPN cloud | An abstraction, not a broadcast domain — real fabric routes per-site subnets via OMP, not L2 flooding (§4.3) |
| Overlay WAN side + both diagrams' LAN switch | Synthetic scaffolding invented by this converter; no vManage counterpart (§4.1, §3.5) |
| `Transport_vpn0` | A label this converter invents (config-driven), not one vManage reports (§3.6) |
| Service VPN names | Template metadata, not running-config; falls back to `VPN <id>` (§4.5) |
| OMP routes / service chaining / control policy | Not modelled — overlay shows VRF membership, not reachability |
| Controllers' path to the transport | A synthesised abstraction (unknown hop count); the single uplink is a best guess ranked from real evidence, not a measurement (§3.7) |
| `device-model` personality inference | Best-effort keyword match, only used when `personality` itself is absent (§5.6) |

**Always validate against vManage before relying on the diagram.**

---

## vManage → Network Sketcher mapping

### Underlay
| vManage | NS |
|------|-----|
| `personality` `vmanage`/`vsmart`/`vbond` | `Server` device (control-plane appliance, not data-plane forwarding) |
| `personality` `vedge` (cEdge/vEdge) | `Router` |
| `device-model` (fallback when `personality` absent) | lower-confidence fallback (0.85 vs. 1.00), flagged in `sdwan_inventory.csv` (§5.6) |
| `site-name` (falling back to `site<id>`) | an **area** (§2.3) |
| VPN0 physical interface resolved via LLDP/CDP | a real **OBSERVED** L1 link (§3.1) |
| VPN0 physical interface grouped by TLOC **colour alone** | one gray **INFERRED** cloud per colour (a star); colour-unresolved → own single-member `unknown_N` cloud, never merged (§3.2) |
| `control/waninterface` `color` of the group | an `l2_segment` named after the colour, cloud side only (§3.4) |
| *(SYNTHESISED)* | one `Dummy 0` SVI per transport cloud, self-bound to its colour segment (§2.4) |
| Live BFD session state (optional) | mesh-shape label suffix only, never a link (§3.3) |
| VPN0 physical interface, L3 instance | `Transport_vpn0` VRF, Edge side only (§3.6) |
| Each Edge's Service VPN LAN side (SYNTHETIC switch) | `Dummy_<n>` switch, identical to the overlay's (§3.5) |
| Service VPN physical/Loopback interface | same as overlay (row below) |
| Control connections (optional), joined by address | ONE L1 link `Dummy_l3` → the single most likely transport cloud (§3.7) |
| *(SYNTHESISED)* | `Dummy_l2` (Switch) + `Dummy_l3` (Router) hops for the controllers' control-plane path (§3.7) |
| Controller's own VPN0 port | real port, real IP where resolved, no VRF (§3.7) |
| Controller VPN 512 port | **NOT drawn** (genuine out-of-band network) |
| Service VPN cloud abstraction | **excluded** — that is the overlay's subject |

### Overlay
| vManage | NS |
|------|-----|
| SD-WAN Edge with ≥1 Service VPN interface | `Router` device, purple, top tier row of its site area |
| Each distinct non-reserved `vpn-id` | one purple Service VPN waypoint cloud in `overlay_wp_`, named from `vpn_names` or `VPN <id>` fallback (§4.5) |
| Each Service VPN an Edge participates in (SYNTHETIC WAN side) | `Vpn <id>` port, L1-linked UP to that VPN's cloud, VPN name as cloud-side L2 segment (§4.1, §4.4) |
| *(SYNTHESISED)* | one `Dummy 0` SVI per Service VPN cloud, self-bound to its VPN segment (§2.4) |
| Each Edge's LAN side (SYNTHETIC switch) | `Dummy_<n>` switch — identical to the underlay's (§3.5) |
| Service VPN physical interface | real IP down to the LAN switch; VRF on both ends (§4.2) |
| Service VPN Loopback | unconnected virtual port (IP + VRF only) (§2.5) |
| VPN 0, Tunnel, transport colours, VPN 512/65528–65530 | **NOT drawn** |
| Real IPsec+BFD mesh, OMP routes, control policy | **NOT drawn** (out of scope) |

## Device color conventions

The generated `rename attribute_bulk` command writes a coloured cell into the
**Default** column of the Network Sketcher Attribute sheet — shared palette
across every converter in this repo:

| Colour | RGB | Meaning | Used in sdwan_converter |
|--------|-----|---------|------------------------|
| 🟥 Light red | `[255, 204, 204]` | **Server-role endpoint** | underlay: vManage / vSmart / vBond |
| 🟩 Light green | `[235, 241, 222]` | **Observed network gear** | underlay: every SD-WAN Edge |
| 🟪 Light purple | `[221, 204, 255]` | **Logical / overlay** | overlay: Edges + Service VPN clouds |
| ⬜ Light gray | `[200, 200, 200]` | **Inferred / synthesised** | underlay: transport clouds, `Dummy_<n>` LAN switches, `Dummy_l3`/`Dummy_l2` hops • overlay: `Dummy_<n>` LAN switches |

Two further fixed cell colours appear in every device row: the **Model**
column is pink `[255, 183, 219]` and the **OS** column is light blue
`[200, 230, 255]`. Note the NS **L3 diagram** additionally tints every
L3-content-bearing device's label with a fixed engine offset — this is
upstream Network Sketcher behaviour, not something this converter emits; see
`DESIGN.md` section 5.3.

## Accuracy caveats

- **Underlay L1 links are OBSERVED only where LLDP/CDP resolve them, else
  INFERRED by TLOC colour** — validated end-to-end only in the
  inference-fallback path so far; the OBSERVED field names are unconfirmed
  (`DESIGN.md` §3.1, §5.4).
- **The L2 segment on every WayPoint cloud is asymmetric (cloud side only)**
  — required so Network Sketcher does not strip the Edge/LAN-switch port's IP
  (§3.4, §4.4).
- **Colour-unresolved VPN0 interfaces never merge with each other** — each
  becomes its own single-member `unknown_N` cloud (§3.2, "Unresolved
  transport colours" above).
- **The `Dummy 0` SVI on each cloud is SYNTHESISED and carries no IP** (§2.4).
- **An underlay area with no L3 interface sorts last** — a workaround for an
  NS engine `KeyError` bug, not a layout preference (§5.2).
- **The `controllers` area is lifted to the TOP row and reversed internally**
  — pure layout, so the control-plane path points down into the transport
  clouds (§3.8); its exact starting column is decided by the NS engine, not
  by this converter (§5.1).
- **The underlay's `Dummy_<n>` LAN switches are SYNTHETIC**, identical to the
  overlay's, from the same shared planner (§3.5).
- **`Transport_vpn0` is a modelling choice**, not a string vManage reports
  (§3.6); its VRF is Edge side only — the transport clouds and the
  controllers get none.
- **Overlay waypoints are an abstraction, not cabling** — one cloud per VPN
  fabric-wide even though real sites have their own subnets (§4.3).
- **Service VPN names are best-effort template metadata**, falling back to
  `VPN <id>` (§4.5).
- **An Edge with no Service VPN interface is absent from the overlay
  entirely** (still present in the underlay); counted in
  `sdwan_overlay_report.md`.
- **Mesh-shape classification requires OPTIONAL `bfd_sessions` data** and is
  an underlay label suffix only, with verified field names (unlike LLDP/CDP)
  (§3.3).
- **WayPoint left-to-right ordering is an ESTIMATE**, not a guaranteed
  pixel-optimal layout (§5.5).

## `fetch_from_vmanage.py` — pulling the model over the REST API

The converter reads local files, but `fetch_from_vmanage.py` grabs the model
straight from a reachable vManage and writes a `convert`-ready combined JSON.
Retrieval is **read-only** (GET only, plus the one unavoidable login POST) —
it never modifies the SD-WAN fabric. Credentials come from a CLI arg or the
`VMANAGE_PASSWORD` env var (never hard-coded, never logged); vManage's
typically self-signed lab certificate is accepted by default
(`--verify-tls` to enforce verification).

Use `--probe` to authenticate, obtain the CSRF token and read only the device
inventory, without collecting per-device detail or writing an export.

It pulls the device inventory, then per device (keyed by `system-ip`) its
interface state, WAN transport-colour state, best-effort LLDP/CDP neighbor
adjacency, and live BFD tunnel-session state; plus one fabric-wide pass for
Service VPN names. See `DESIGN.md` section 6.2 for the full endpoint list and
which JSON field each one populates. Each per-device query is independent and
a failed one is **skipped, not fatal** — a partial fetch still produces the
best diagram the data allows.

`convert.py` also accepts a directory of per-endpoint `*.json` dumps. See
`DESIGN.md` section 6 for the complete combined-JSON schema (including an
illustrative LLDP-neighbor record shape).

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
> **reuse the stale diagrams**. Follow the rules below to avoid it.

**Rule 1 — one mode = one clean, dedicated workspace.** Never reuse a
workspace across modes or runs. The workspace must be under your **home
directory** (the engine rejects paths outside it).

**Rule 2 — ordered steps, per mode** (`underlay`, then `overlay`):
1. Use a **fresh empty workspace directory for this mode**. If reusing a
   directory, first delete every prior artifact in it: `[MASTER]*.nsm`,
   `[L1_DIAGRAM]*`, `[L2_DIAGRAM]*`, `[L3_DIAGRAM]*`, `[L1L2L3_DIAGRAM]*`,
   `[DEVICE_TABLE]*`, `[AI_Context]*`.
2. `create_empty_master` → `[MASTER]no_data.nsm`.
3. `run_commands` with this mode's `*.txt`. Pass the commands as a
   **newline-delimited single string with the `#` comment lines removed** —
   NOT a JSON array. Expect `N OK, 0 FAIL`; if any command FAILs, stop and
   fix before continuing.
4. `build_default_outputs` → expect `Summary: 6/6 succeeded.`
5. **Immediately move/rename** the six artifacts out of the workspace,
   tagged by mode, before touching the next mode.

**Rule 3 — verify before trusting the result (do all three):**
- **Content marker:** the `underlay` L2 SVG must contain a controller name,
  the `Dummy_l3`/`Dummy_l2` hops, and a transport cloud named after a TLOC
  colour (optionally suffixed `(Full Mesh)`/`(Hub-and-Spoke)`/
  `(Partial Mesh)`); the `overlay` L2 SVG must contain a Service VPN cloud
  name (e.g. `Corporate`, or `VPN 10`) and none of those. The `Dummy_<n>` LAN
  switches are **not** a usable marker — both modes draw the same ones.
- **Cross-mode diff:** the `underlay` and `overlay` `[L1L2L3_DIAGRAM]` HTML
  files must **not** be byte-identical.
- **Freshness:** each artifact's modification time must be newer than the
  moment you started this mode's build.

If any check fails, the workspace was dirty — redo this mode in a brand-new
empty workspace (Rule 1).

## Configuration (`sdwan_to_ns_config.json`)

Every key is `{value, description, sample}`; only `value` is read. Key
options: `device_naming` (`hostname` | `hostname_ip`), `unknown_color_label`
(base label for a colour-unresolved VPN0 interface, default `"unknown"` —
see "Unresolved transport colours" above), `enable_observed_l1_links`
(underlay; default `true`), `prefer_lldp_over_cdp` (underlay; default
`true`), `excluded_service_vpns` (**both modes**; default
`[512, 65528, 65529, 65530]` — VPN 0 is always excluded from the Service VPN
side unconditionally), `transport_vpn_name` (underlay; default
`"Transport"` — blank it for a bare `vpn0`), `vpn_name_fallback_format`
(overlay; default `"VPN {id}"`), `vrf_name_format` (**both modes**; default
`"{name}_vpn{id}"`), and `lan_dummy_name_format` (**both modes**; default
`"Dummy_{n}"`).

Three further keys are underlay-only and govern the controllers'
control-plane path: `enable_controller_control_plane_links` (default
`true`), `control_l3_dummy_name` (default `"Dummy_l3"`) and
`control_l2_dummy_name` (default `"Dummy_l2"`).

## Directory structure

```
sdwan_converter/
├── README.md                      (this file)
├── DESIGN.md                      (full design, worked examples, JSON schema)
├── requirements.txt
├── sdwan_to_ns_config.json        (sole configuration file)
├── Input_data/
│   └── sample_sdwan_export.json   (bundled sample — sanitized real 12-device export)
├── Output_data/                   (default output; gitignored)
├── tools/
│   └── sanitize_vmanage_export.py (regenerates the bundled sample from a real export;
│                                   idempotent, and refuses to write its output —
│                                   non-zero exit, offending JSON paths on stderr —
│                                   if anything environment-specific survives the pass)
└── src/                           (entry point: convert.py; also fetch_from_vmanage.py)
```

## Author

Yusuke Ogawa - Architect, Cisco | CCIE#17583

## License

This tool is part of the **Network Sketcher Cisco Extension** project, licensed
under the [Apache License 2.0](../LICENSE). See the [NOTICE](../NOTICE) file for
copyright and third-party attributions.
