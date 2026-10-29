# fmc_converter — Secure Firewall Management Center to Network Sketcher

Convert a [Cisco Secure Firewall Management Center](https://www.cisco.com/site/us/en/products/security/firewalls/index.html)
deployment into a ready-to-run
[Network Sketcher](https://github.com/cisco-open/network-sketcher) command
script. Each firewall becomes its own area. A high-availability pair stays in
one area, with the two firewalls side by side. Interface direction is drawn
with grey switches that are not real devices: management and outside above
the firewall, DMZ to the right, and inside — plus any interface whose name
matches no direction keyword — along the bottom.

Data acquisition is **API-based**: a read-only client pulls FMC into a JSON
file, and the converter turns that local file into commands. Conversion does
not connect to FMC.

```
FMC REST API  --(read-only)-->  fmc_export.json
                                      |
                                      v
                            ns_commands_fmc.txt  -->  Network Sketcher
```

## Overview

| Item | Detail |
|------|--------|
| **Input** | FMC read-only REST export → `fmc_export.json` |
| **Output** | `ns_commands_fmc.txt` plus a model, an inventory CSV, and a direction report |
| **Dependencies** | Python 3.10+, **standard library only** |
| **FMC connectivity** | Only for the fetch step. Conversion reads the JSON file. |

## Quick Start

```bash
pip install -r fmc_converter/requirements.txt

# Fetch (password via FMC_PASSWORD, or pass --password).
# TLS verification is off by default because FMC appliances commonly use a
# private certificate. Pass --verify-tls when the certificate is trusted.
FMC_PASSWORD=... python -m fmc_converter.src.fetch_from_fmc \
    --host fmc.example.com --user admin \
    --out fmc_converter/Input_data/fmc_export.json

# Confirm the account can read FMC without writing a file:
FMC_PASSWORD=... python -m fmc_converter.src.fetch_from_fmc \
    --host fmc.example.com --user admin --probe

python -m fmc_converter.src.convert \
    -i fmc_converter/Input_data/fmc_export.json \
    -o fmc_converter/Output_data/ns_commands_fmc.txt \
    -c fmc_converter/fmc_to_ns_config.json
```

Run the non-comment lines of `ns_commands_fmc.txt` in a Network Sketcher
master, in the order they appear.

## What the diagram shows

The top area contains FMC and, beneath it, a grey switch named `Dummy_fmc`.
FMC is cabled to that switch. The middle area is a single waypoint, `wan_wp_`,
holding `Dummy_wan`. Firewall areas sit in one row underneath, ordered by
hostname, and are named `Dummy_Area_1`, `Dummy_Area_2`, and so on. The
firewall icon keeps the hostname. `/`, `\`, and `'` in a hostname become `_`.
FMC itself is labelled with its hostname.

The grey switch uses the same interface type and number as the firewall port
it faces. A Management port is the exception: the switch side is always
GigabitEthernet with that same number.

Each firewall's management switch connects to `Dummy_wan`, and `Dummy_wan`
connects to `Dummy_fmc`. Firewall areas are not cabled to each other or
directly to the FMC area. An HA pair shares one `Dummy_Area_N`. When both
members have one failover interface, those interfaces are cabled to each other.

An EtherChannel is drawn from its physical members and recorded as a
port-channel. The address, when FMC has one, is placed on `Port-channel N`.
A sub-interface keeps its address and VLAN on the parent port. A disabled
port with no interface name and no security zone is omitted.

## Output files

| File | Description |
|------|-------------|
| `ns_commands_fmc.txt` | Network Sketcher CLI commands (Phase 1–6) |
| `ns_model_fmc.json` | Intermediate topology model |
| `fmc_inventory.csv` | Device → stencil mapping |
| `fmc_report.md` | Counts, the direction chosen for each interface, and skipped ports |

## Device color conventions

| Colour | RGB | Meaning |
|--------|-----|---------|
| Light green | `[235, 241, 222]` | The firewall, as observed on FMC |
| Light red | `[255, 204, 204]` | FMC itself (Server stencil) |
| Light gray | `[200, 200, 200]` | Synthesised switches and the `Dummy_wan` waypoint |

`Dummy_wan` is a WayPoint. The grey switches inside a firewall area are
devices, coloured grey because they are not real. The Model column is pink
`[255, 183, 219]` and the OS column is light blue `[200, 230, 255]`.

## Fetch

`fetch_from_fmc` signs in with the FMC username and password, then only reads.
The password is taken from `--password` or the `FMC_PASSWORD` environment
variable and is not written to the export or the log. Device registration
keys (`regKey`, `natID`), the HA failover `sharedKey` and any password field
are removed from the export as well. TLS certificate
verification is **off** unless you pass `--verify-tls`. `--probe` checks the
login and the server version, then exits without writing a file.

The export contains the FMC version, the device records, HA pairs, each
device's interfaces, and its IPv4 and IPv6 static routes. A default route
(`0.0.0.0/0` or `::/0`) is drawn as the address of the grey switch facing that
egress interface, in the prefix of the interface address of the same family.
An IPv6 default route whose next hop is link-local (`fe80::/10`) is not drawn,
because Network Sketcher does not accept link-local addresses; the report
lists it.

Interfaces carry their static IPv4 address and their static IPv6 addresses
(dual stack). The IPv6 link-local address, EUI-64 addresses, SLAAC/DHCPv6 and
router-advertisement prefixes are not drawn.
The firewall's management interface often has no IPv4 address in this API;
the device hostname is a separate field. The FMC appliance's own management
address and default gateway are not in the REST API (they are CLI settings),
so `Dummy_fmc` receives that gateway only when `ipv4` and `gateway` are
present on the FMC object in the export.

## Configuration (`fmc_to_ns_config.json`)

Every key is `{value, description, sample}`. Only `value` is read. Matching is
case-insensitive and uses the interface name, the security zone, and the
hardware name. The first list that matches wins.

| Key | Effect |
|-----|--------|
| `management_keywords` | Drawn above the firewall. A `managementOnly` interface is always management. |
| `top_keywords` | Drawn above the firewall (outside, internet, wan, and similar). |
| `right_keywords` | Drawn to the right (DMZ). |
| `bottom_keywords` | Drawn along the bottom. Anything that matches no list is also drawn there. |

Add a word to the matching list to change where a name is drawn. No code
change is required.

## Changelog

- **Fixed: a sub-interface VLAN binding is written `'Vlan<id>'`**, quoted
  (`add vport_l2_direct_binding`). The lower-case, unquoted `vlan<id>` never
  joined the switch's `Vlan<id>` segment and is rejected by the Network
  Sketcher MCP command validator.

## Author

Yusuke Ogawa - Architect, Cisco | CCIE#17583

## License

This tool is part of the **Network Sketcher Cisco Extension** project,
licensed under the [Apache License 2.0](../LICENSE). See the
[NOTICE](../NOTICE) file for copyright and third-party attributions.
