# Input_data/sample1/

Place `show running-config` text files here — one device per file, or
multiple devices concatenated in one file (auto-split, see DESIGN.md section
4.1.1 decision 12). Any file extension is accepted (no whitelist); only a
short list of obviously-binary extensions is skipped outright, plus this
directory's own `README.md` (matched by filename, not extension — see
`convert.py`'s `_is_repo_documentation_file()`). See `config_converter/
DESIGN.md` section 4.1/4.1.1 for the full rule.

## Bundled Phase 1a sample (IOS / IOS-XE only)

RFC 5737 / RFC 1918 synthetic sample configs, added for Phase 1a (DESIGN.md
section 6):

| File | Device(s) | Notes |
|---|---|---|
| `ios_core_rtr01.txt` | `CORE-RTR01` (IOS) | WAN edge router: `GigabitEthernet0/0` is the WAN uplink (203.0.113.0/30, TEST-NET-3) with `ip nat outside`, an inbound ACL, external eBGP (AS65001 -> AS64512), and a `shape average` bandwidth limit via `service-policy`. `GigabitEthernet0/1` is a direct point-to-point link (10.255.255.0/30) to `DIST-SW01` — a 2-device shared-subnet example. |
| `ios_dist_sw01.txt` | `DIST-SW01` (IOS) | L3 distribution switch; `Vlan10` SVI (10.10.10.0/24) is HSRP **active** (virtual IP 10.10.10.1); trunk uplink to `ACCESS-SW01`. |
| `ios_access_sw01.txt` | `ACCESS-SW01` (IOS) | Access switch; trunk uplink, access ports (incl. one `shutdown` port), a `police`-based bandwidth limit on a guest port, and a `Vlan10` SVI (10.10.10.10) — the 3rd device sharing the 10.10.10.0/24 subnet with the two distribution switches below (k>=3 same-subnet example, for Phase 2's L2-inference gate). |
| `iosxe_dist_sw02_edge_rtr03.txt` | `DIST-SW02` + `EDGE-RTR03` (both IOS-XE) | **Two devices concatenated in one file** (multi-device split example, decision 12). `DIST-SW02`'s `Vlan10` SVI (10.10.10.3) is HSRP **standby** for the same 10.10.10.0/24 subnet/virtual IP as `DIST-SW01`. `EDGE-RTR03` is a small, otherwise-unconnected router with a secondary/backup WAN-looking interface (198.51.100.0/30, TEST-NET-2). |

## Bundled Phase 1b sample (NX-OS)

| File | Device(s) | Notes |
|---|---|---|
| `nxos_core01.txt` | `NXOS-CORE01` (NX-OS) | `!Command: show running-config` banner (NX-OS's own device-boundary marker, distinct from IOS/IOS-XE). `Vlan10` SVI (10.10.10.4/24) joins the **same** 10.10.10.0/24 subnet as `DIST-SW01`/`DIST-SW02`/`ACCESS-SW01` above (now a 4-real-candidate group, for Phase 2's k>=3 matching). `Vlan30` SVI demonstrates NX-OS's **nested** `hsrp 30` / `ip 10.30.30.1` virtual-IP syntax (as opposed to IOS/IOS-XE's single-line `standby <group> ip <addr>`). `Ethernet1/1` is a routed (`no switchport`) spare uplink with no known peer in this sample (198.51.100.8/30). `Ethernet1/2` is a `shutdown` access port. `mgmt0` is in the `MGMT` VRF. `ip access-list MGMT-IN` uses NX-OS's sequence-numbered rule syntax (`10 permit ...` / `20 deny ...`) with no `standard`/`extended` type keyword. External eBGP peer (AS65010 -> AS65099). |

## Bundled Phase 1c sample (IOS-XR)

| File | Device(s) | Notes |
|---|---|---|
| `iosxr_edge01.txt` | `EDGE-XR01` (IOS-XR) | `!! IOS XR Configuration` banner. `Bundle-Ether1` (IOS-XR's LAG, `ipv4 address 10.255.255.20/30`) with two `GigabitEthernet0/0/0/0`/`.../1` members joined via `bundle id 1 mode active` (IOS-XR's `channel-group` equivalent). `GigabitEthernet0/0/0/2` is a WAN hand-off (203.0.113.9/30, TEST-NET-3) with `ipv4 access-group SAMPLE-ACL ingress` (IOS-XR's `ip access-group ... in` equivalent). `ipv4 access-list SAMPLE-ACL` uses sequence-numbered rules (`10 permit ...` / `20 deny ...`, has an explicit permit so it is NOT bidirectional-deny-all). `MgmtEth0/RP0/CPU0/0` is in the bare top-level `vrf MGMT`. `router bgp 65020` uses IOS-XR's nested `neighbor <ip>` / `remote-as <asn>` two-line form (AS65020 -> AS65099 external peer). |

## Bundled Phase 1d sample (ASA(FTD/FDM))

| File | Device(s) | Notes |
|---|---|---|
| `asa_fw01.txt` | `ASA-FW01` (ASA) | `: Saved` / `ASA Version` banner. `GigabitEthernet0/0` has `nameif outside` / `security-level 0` (203.0.113.13/30, TEST-NET-3); `GigabitEthernet0/1` has `nameif inside` / `security-level 100` (10.40.40.1/24); `GigabitEthernet0/2` is `shutdown` with no `nameif`. `access-list OUTSIDE-IN extended deny ip any any` (single explicit deny, no permit -> bidirectional-deny-all) and `access-list INSIDE-OUT extended permit ip any any` are declared **outside** any interface block and bound via the ASA-specific *global* `access-group <name> in interface <nameif>` command, resolved back onto each interface by name via its `nameif`. `object network INSIDE-NET` / `object network DMZ-SERVER1` are collected as existence-only NAT object names (full NAT/object resolution is out of scope, DESIGN.md 4.2.1). `router bgp 65030` uses ASA's classic single-line `neighbor <ip> remote-as <asn>` form (AS65030 -> AS65099 external peer). FTD (FMC- or FDM-managed) exposes this exact same ASA/LINA-syntax text export and is parsed via this identical code path — the tool treats ASA/FTD/FDM as one unified family displayed as **"ASA(FTD/FDM)"** (see DESIGN.md section 4.2.1.2 for the MATCHA-sourced investigation behind this decision). No separate FTD/FDM sample is bundled, since the parsed syntax is identical. |

All five Phase-1-scope OS families (IOS, IOS-XE, NX-OS, IOS-XR, and the
unified ASA(FTD/FDM) family) now have bundled synthetic samples; Phase 1e
onward reuses this same sample set for topology-mapping and stencil-mapping
verification (DESIGN.md section 6).

## Phase 1e verification result (topology_mapper + stencil_mapper + MCP)

Running `convert.py` against this entire bundled sample set (all 7 files / 13
devices) produces `devices=13 l1_links=16`: `CORE-RTR01`/`ASA-FW01`/
`EDGE-XR01`/`EDGE-RTR03` each link their WAN-scored interface to the shared
`Dummy_CL_1` cloud waypoint; `DIST-SW01`/`DIST-SW02`/`NXOS-CORE01`/
`ACCESS-SW01` (all 4 sharing the 10.10.10.0/24 `Vlan10` subnet) star-connect
to a synthesized `Dummy_L2_1` switch (flagged `ambiguous=True` per the Phase
1e k>=3 simplification, see DESIGN.md section 6 Phase 2); `EDGE-XR01`'s
`MgmtEth0/RP0/CPU0/0` and `NXOS-CORE01`'s `mgmt0` (both in a management VRF)
star-connect to a second synthesized `Dummy_L2_2`; `ASA-FW01`'s inside
interface and `NXOS-CORE01`'s unmatched `Vlan30` each get a generic
`Dummy_RT_<n>` peer; `CORE-RTR01` <-> `DIST-SW01` is a direct point-to-point
link. This was verified end-to-end against a live Network Sketcher master via
the `user-network-sketcher` MCP server (`create_empty_master` ->
`get_ai_context` -> `run_commands` for all 6 generated NS commands ->
`build_default_outputs`), and the exported L1 diagram visually confirms the
topology described above, including all `Dummy 0`-`Dummy 6` port names and
the `Bundle-Ether 1` port. See DESIGN.md section 6 (Phase 1e row) and section
5 (risks #12, #15, #16) for the full verification narrative and the two bugs
this live-engine test uncovered and fixed (`Bundle-Ether` port normalisation,
`stencil_mapper` Model-string quoting).

> [!NOTE]
> **Post-Phase 2 update**: the counts above (`l1_links=16`, synthetic
> `Dummy_L2_1` for the 4-device `Vlan10` group) reflect the Phase 1e
> simplification that always fell back to a synthesized switch for k>=3
> groups. With Phase 2's full k>=3 resolution implemented (DESIGN.md section
> 4.3.6), running the same sample set now reports `l1_links=18` and resolves
> that group via `best_pair` degree-similarity matching (no `Dummy_L2_1` is
> synthesized for this particular sample, since none of the 4 real
> candidates is structurally identifiable as a `real_hub`). See
> `DESIGN.md` section 4.3.6 for the `real_hub` strategy and k>=3 matching
> behaviour exercised by larger multi-device corpora.
