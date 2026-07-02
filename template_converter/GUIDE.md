# Building a new converter — authoring guide

This guide is for anyone — an AI coding agent or a human — building a new
"Platform X → Network Sketcher" converter for this repository, whether that's
an official Cisco platform or a third-party product. Every converter here
(`aci_converter`, `catc_converter`, `cml_converter`, `cv_converter`,
`meraki_converter`, `nd_converter`, `sna_converter`, and the community
`3rd_party/netbox_converter`) was built independently by an AI agent. Reading
this guide *before* you start prevents the class of mistakes a previous
session had to find and fix after the fact: a sample-filename mismatch that
left a converter with zero working example, a missing `.gitignore` rule that
left real user data unprotected, inconsistent "what's observed vs. inferred"
documentation, and internal/contributor-facing prose leaking into a
user-facing README.

**Read this whole file before writing any code.** It is not long, and every
rule in it exists because of a real inconsistency found across the 8
converters already in this repo.

## 1. Where to place the new converter

- **Cisco platform** (e.g. a new Catalyst SD-WAN or Secure Firewall
  Management Center converter) → `<platform>_converter/` at the repository
  root, as a sibling of `aci_converter/`, `nd_converter/`, etc.
- **Non-Cisco / third-party platform** (e.g. Fortinet, Juniper, Palo Alto,
  anything that isn't a Cisco product) → `3rd_party/<platform>_converter/`,
  as a sibling of `3rd_party/netbox_converter/`.

The only difference this makes is folder placement and one path segment in
your module imports (`3rd_party` can't be a Python package name — it starts
with a digit — so third-party converters are run as
`cd 3rd_party && python -m <platform>_converter.src.convert ...`, exactly
like `netbox_converter` already does). The internal architecture below is
identical either way.

## 2. The canonical architecture

Copy `template_converter/` as your starting point:

```
<platform>_converter/
├── README.md                        (from README.template.md — fill in every <PLACEHOLDER>)
├── requirements.txt
├── .gitignore
├── <platform>_to_ns_config.json     (from PLATFORM_to_ns_config.json.template)
├── Input_data/.gitkeep
├── Output_data/.gitkeep
└── src/
    ├── __init__.py
    ├── ns_model.py                       ← COPY VERBATIM, do not edit the body
    ├── ns_command_builder.py             ← COPY VERBATIM except the stencil-mapper import
    ├── convert.py                        (from convert.py.template)
    ├── fetch_from_<platform>.py          (from fetch_from_platform.py.template — DELETE if no API)
    ├── <platform>_reader.py              (from platform_reader.py.template)
    ├── <platform>_mapper.py              (from platform_mapper.py.template)
    └── <platform>_stencil_mapper.py      (from platform_stencil_mapper.py.template)
```

**What's copy-verbatim vs. what's platform-specific — verified, not assumed:**
I compared `ns_model.py` and `ns_command_builder.py` across `aci_converter`,
`catc_converter`, `nd_converter`, and `3rd_party/netbox_converter`. Their own
docstrings say it plainly — `nd_converter/src/ns_model.py`: *"shared verbatim
with `aci_converter` / `cml_converter`"*; `nd_converter/src/ns_command_builder.py`:
*"sibling copy of `aci_converter/src/ns_command_builder.py` ... The body is
identical"*. `template_converter/src/ns_model.py` and `ns_command_builder.py`
are that same verbatim copy (taken from `nd_converter`, the most recently
validated one). **Copy them into your new converter unmodified.** The
*only* edit either file should ever need is repointing the
`platform_stencil_mapper` import at your new converter's real stencil-mapper
module name.

Everything else is real work specific to your platform:

- **`fetch_from_<platform>.py`** (only if the platform has a read-only API —
  delete this file and skip step 2 of the Quick Start if it doesn't, like
  `cml_converter`/`cv_converter`/`sna_converter`, which only take a local
  file). See §3 below for the required credential/security pattern.
- **`<platform>_reader.py`** — parses the raw export/API JSON into a typed
  `PlatformData` object with lookup indexes. No topology logic here.
- **`<platform>_mapper.py`** — the real work: turns `PlatformData` into an
  `NSModel` (devices, links, VLANs, IPs, VRFs, port-channels — whatever your
  platform has). If your platform deliberately separates a physical
  fabric/campus from a logical policy overlay that doesn't map onto it 1:1
  (VXLAN EVPN, SD-Access, ACI's Tenant/VRF/BD/EPG model, ...), split this into
  `<platform>_physical_mapper.py` + `<platform>_logical_mapper.py` and give
  `convert.py` a `--mode underlay|overlay|both` flag — copy the exact shape
  from `aci_converter/src/`, `catc_converter/src/`, or `nd_converter/src/`.

  **Observed vs. inferred WayPoints — get this right the first time.** A
  WayPoint's colour is decided by whether it's backed by a real record in
  your source data, NOT by whether it's a "managed"/"simulated" device.
  `catc_converter`'s and `nd_converter`'s "unmanaged neighbour" nodes and
  `cml_converter`'s `external_connector` node all come from a real API
  field or a real user-authored lab node (a real label/IP/sysName) — they
  are **observed**, so they must get `default_color=(220, 230, 242)`
  (NS's native WayPoint light blue), the same way
  `3rd_party/netbox_converter`'s WAN/provider-circuit WayPoints do. Reserve
  gray `(200, 200, 200)` for a WayPoint your converter *invents itself* with
  no real record behind it at all (e.g. `meraki_converter`'s synthesized
  `Internet` cloud, `sna_converter`'s traffic-inferred WAN cloud). Getting
  this backwards was a real bug found and fixed in three converters in this
  repo — don't repeat it.
- **`<platform>_stencil_mapper.py`** — maps your platform's device
  role/type strings to one of the 10 fixed NS stencil types
  (`Router`/`L3Switch`/`Switch`/`Firewall`/`WLC`/`AP`/`Server`/`Cloud`/`Phone`/`PC`).
  These 10 constants and the `StencilMapping` dataclass shape are fixed —
  copy them; only the role-lookup table is platform-specific.
- **`convert.py`** — the CLI entry point wiring reader → mapper → `NSModel` →
  `ns_command_builder` → output files. Copy `convert.py.template`'s
  single-mode shape for most converters; copy `nd_converter/src/convert.py`'s
  `--mode` shape only if you did the physical/logical mapper split above.

**For a complete, clean worked example, read `3rd_party/netbox_converter/src/`
end-to-end.** It's the newest single-mode converter in this repo and
demonstrates the whole pattern clearly (`fetch_from_netbox.py` →
`netbox_reader.py` → `netbox_mapper.py` + `netbox_stencil_mapper.py` →
shared `ns_model.py`/`ns_command_builder.py`). For the dual-mode
(underlay/overlay) shape, read `nd_converter/src/` — it's the most recently
validated of the three converters that use it.

## 3. Credential & security conventions

Verified across every fetch script in this repo (`fetch_from_apic.py`,
`fetch_from_catc.py`, `fetch_from_meraki.py`, `fetch_from_nd.py`,
`fetch_from_netbox.py`) — follow all of these:

- **Credentials: CLI arg first, environment variable fallback, never
  hardcoded.** `password = args.password or os.environ.get("<PLATFORM>_PASSWORD")`
  (or `<PLATFORM>_API_KEY` / `<PLATFORM>_TOKEN` for token-auth platforms).
- **Standard library only** — `urllib` + `ssl`. No `requests`, no vendor SDK.
  Every converter in this repo except `cml_converter` (PyYAML) and
  `3rd_party/netbox_converter` (networkx, for its placement graph) has zero
  third-party dependencies; keep that bar unless there's truly no reasonable
  stdlib way to do the job.
- **Read-only / GET-only.** The fetch script must never be able to modify the
  platform. State this explicitly in your README (see the template).
- **TLS verification default — pick the one that matches your platform's
  real-world deployment, don't default a public cloud API's verification
  off:**
  - Self-signed on-prem/lab controllers (APIC, Catalyst Center, Nexus
    Dashboard) default **verification OFF** with a `--verify-tls` flag to
    enable it.
  - Public cloud APIs (Meraki Dashboard, a public NetBox instance) default
    **verification ON** with a `--no-verify-tls` flag to disable it.
- **Never log the credential itself** — a success message like
  `"[ok] authenticated to {host} as {user}"` is fine; the token/password
  never is.

## 4. `Input_data`/`Output_data`/`.gitignore` convention

Every converter's `Input_data/` must ship **exactly the minimum set of
sample file(s)** needed to run the tool out of the box — one file for most
converters, but it's fine to need more if your platform's format genuinely
requires it (e.g. `cv_converter` needs a nodes CSV *and* an activities CSV).
`Output_data/` must ship **zero** generated-content files (only the
structural `.gitkeep` placeholder that keeps the empty folder visible after
a clone).

Use `template_converter/.gitignore` as your starting point: `Input_data/*`
ignored except `.gitkeep` and each real sample filename listed explicitly
(never whitelist a whole pattern like `sample_*` — list each file by exact
name, so a real export a user drops in next to the sample doesn't
accidentally get whitelisted too). `Output_data/*` ignored except
`.gitkeep`.

**Before committing any sample file, verify it's safe to publish:**
- Public vendor sandbox/demo data is fine (e.g. `meraki_converter`'s sample
  is real DevNet Always-On Sandbox data; `catc_converter`'s is real Cisco
  dCloud sandbox data — both use non-routable/documentation IP ranges and
  sandbox-only hostnames).
- Fully sanitized synthetic data is fine (e.g. `nd_converter`'s sample has
  its IPs replaced with RFC 5737 documentation addresses and serials replaced
  with `DUMMYSER*` placeholders, and says so in its own `_meta` field).
- **Never** commit a real customer/production export.

Verify your `.gitignore` actually does what you intend with:
```bash
git check-ignore -v <platform>_converter/Input_data/*
git check-ignore -v <platform>_converter/Output_data/*
```
The only files that should come back as *not* ignored are your sample
file(s) and the two `.gitkeep`s.

## 5. README convention

Copy `README.template.md` and fill in every `<PLACEHOLDER>`. Required
sections: intro, Overview table, Quick Start, Output files, Device color
conventions, Configuration, Author, License. Optional sections — only add
these if your converter genuinely has the dual underlay/overlay shape:
Modes table, `<Platform> → Network Sketcher mapping` tables, "Concepts that
can NOT be fully represented", and an AI-agent runbook (copy verbatim from
`aci_converter`/`catc_converter`/`nd_converter` — it documents a real
Network Sketcher MCP engine caching quirk, not something to reinvent).

**The hard rule, learned from a real cleanup pass over every README in this
repo: content must be strictly user-facing.** The intended reader is
"someone who wants to run this converter against their own data/API and get
a diagram" — not a contributor reading your source code. Concretely, do
**not** include:
- Internal module/function names or a "How it works" pipeline diagram
  naming your `.py` files (a user runs a CLI command; they don't need to
  know `platform_mapper.py` calls `platform_reader.py`).
- A `Directory structure` tree that enumerates everything inside `src/`.
- Contribution/PR-process notes ("Getting involved", dev-environment setup)
  — that belongs in `CONTRIBUTING.md` only.
- Internal QA-methodology narrative — e.g. "validated against a synthetic
  export because our lab didn't have this feature enabled," a per-endpoint
  verification matrix citing raw API paths, or "field names vary across
  releases so the parsers are tolerant." A user cares about *is it reliable*,
  not *how was it tested* — say "validated end-to-end against a live
  `<platform>`" and stop there.
- "Please report any export that does not parse" or similar
  maintainer-solicitation phrasing.

What's genuinely fine to keep: a plain reliability statement, "always
validate against `<platform>` before relying on the diagram" (risk
disclosure to the user, not maintainer caveat), and an explanation of *why a
diagram looks the way it does* when that's needed to interpret it correctly
(e.g. "this converter shows the observed physical fabric, not an inferred
one, because the platform's API returns real topology data").

## 6. Top-level `README.md` integration checklist

Once your converter works end-to-end:

1. Add a row to the correct table in the top-level `README.md` — the main
   "Tools in this extension" table for a Cisco platform, or the "Community /
   third-party converters" table for a non-Cisco one — in **strict
   alphabetical order by Product name**.
2. Add a `## Tool N — <platform>_converter` section (Cisco) or a short
   Installation/Usage entry (third-party) following the exact pattern of the
   existing sections — reuse your own README's real commands, don't invent
   new ones for the summary.
3. Add a line to the "Repository structure (monorepo)" tree — folder name,
   README/requirements/config/Input_data/Output_data only, **no `src/`
   internals** (same user-facing-only rule as §5).
4. Add a bullet to "Credits and references" linking the platform's official
   product page.
5. Do **not** add `template_converter/` itself to any of this — it's not a
   tool, it's contributor tooling. It belongs only in `CONTRIBUTING.md`.

## 7. Pre-push checklist

Run through this before committing/pushing your new converter — every item
here is a real mistake found and fixed in an earlier converter in this repo:

- [ ] `.gitignore` correctness verified with `git check-ignore -v` (§4) —
      exactly your sample file(s) + two `.gitkeep`s are trackable, nothing
      else.
- [ ] Every sample file's content reviewed for safety (§4) — no real
      customer data, no credentials, no internal hostnames beyond a
      documented sandbox/demo source.
- [ ] README read back against §5's user-facing-only rule.
- [ ] Top-level `README.md` row/section added in alphabetical order (§6).
- [ ] No hardcoded credentials anywhere — `grep -rn "password\s*=" src/` and
      confirm every hit reads from a CLI arg or env var.
- [ ] `<platform>_to_ns_config.json` uses the `{value, description, sample}`
      shape, consistent with every other converter's config file.
- [ ] `requirements.txt` states plainly whether install is a no-op (stdlib
      only) or lists real packages with a one-line reason.
