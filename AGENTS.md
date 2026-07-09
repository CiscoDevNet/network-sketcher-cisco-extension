# AGENTS.md

Guidance for AI coding agents (VS Code, GitHub Copilot, Cursor, Codex, Gemini CLI, etc.) working in
this repository.

**What this project is:** `network-sketcher-cisco-extension` is a monorepo of standalone Python CLIs
that turn Cisco platform data into [Network Sketcher](https://github.com/cisco-open/network-sketcher)
CLI command scripts, so an L1/L2/L3 topology can be rebuilt automatically. Each tool lives in its own
sub-directory (`aci_converter/`, `cml_converter/`, `config_converter/`, `cv_converter/`,
`sna_converter/`, …) with its own
`README.md` and `requirements.txt`, and can be used independently. **Conversion runs entirely on local
files** — no live platform connection is needed at conversion time. Always read the root `README.md`
and the relevant tool's `README.md` before making changes.

## Dev environment tips

- **Python version**: Use Python 3.10+ (works for all tools). `cv_converter` and `sna_converter` also
  run on 3.8+. `aci_converter`, `cv_converter`, and `sna_converter` use the **standard library only**;
  `cml_converter` needs `PyYAML` (optionally `ciscoconfparse2`); `config_converter` needs
  `networkx` (optionally `ciscoconfparse2`).
- **Virtual env (recommended)**:
  ```bash
  python3 -m venv .venv
  source .venv/bin/activate          # Windows: .\.venv\Scripts\Activate.ps1
  python -m pip install -U pip
  ```
- **Per-tool dependencies** (install only what the tool you are touching needs):
  ```bash
  pip install -r cml_converter/requirements.txt   # PyYAML (+ optional ciscoconfparse2)
  pip install -r aci_converter/requirements.txt   # no-op (stdlib only)
  pip install -r config_converter/requirements.txt   # networkx (+ optional ciscoconfparse2)
```

### Quick run examples

```bash
# aci_converter — convert a (bundled, synthetic) APIC export into underlay + overlay command scripts
python -m aci_converter.src.convert \
    -i aci_converter/Input_data/sample_export.json \
    -m both -o aci_converter/Output_data/ns_commands.txt

# cml_converter — convert a CML lab YAML (+ embedded running-configs) into NS commands
python -m cml_converter.src.convert --yaml /path/to/your_lab.yaml --out /tmp/ns_commands.txt

# config_converter — reconstruct L1/L2/L3 from Cisco running-config text files
python -m config_converter.src.convert \
    -i config_converter/Input_data/sample1/ \
    -o config_converter/Output_data/ns_commands.txt

# cv_converter — build an OT (Purdue/IEC 62443) topology from Cyber Vision CSV exports
cd cv_converter && python cv_to_ns_commands.py            # auto-detects CSVs in Input_data/

# sna_converter — reconstruct a topology + [FLOW] matrix from a NetFlow CSV (bundled sample)
cd sna_converter && python sna_to_ns_commands.py          # uses Input_data/sample_flows.csv
```

Each tool writes a plain-text `ns_commands.txt` (one Network Sketcher CLI command per line; `#` lines
are phase comments) plus supporting reports/CSVs into that tool's `Output_data/`.

## Testing instructions

- **No automated test suite yet.** Validate changes by running the affected tool against a bundled
  sample (`aci_converter/Input_data/sample_export.json`,
  `config_converter/Input_data/sample1/`, `sna_converter/Input_data/sample_flows.csv`)
  or your own export, and confirm the emitted `ns_commands.txt` and reports look correct.
- **Run the output in Network Sketcher (MCP server).** The generated commands are meant to be executed
  against a Network Sketcher master file. If a `network-sketcher` MCP server is available, use it to
  create an empty master, run the non-comment lines of `ns_commands.txt` in Phase 1→6 order, and export
  the L1/L2/L3 diagram + device table to verify the topology. Otherwise install and run Network
  Sketcher from https://github.com/cisco-open/network-sketcher.
- **Cisco DevNet sandbox** (for the read-only fetch helpers, e.g. `aci_converter`'s `fetch_from_apic`):
  book an ACI / APIC sandbox at https://devnetsandbox.cisco.com/DevNet.
- **Latest Cisco API documentation**: https://developer.cisco.com/docs/

## PR instructions

- **Security**: Do not commit real credentials, tokens, or real platform exports. Read secrets from
  environment variables (e.g. `ACI_PASSWORD`) and document any required env vars or input files. Use
  synthetic/sanitized sample data (the bundled samples use RFC 5737 / RFC 1918 documentation IPs).
- Add the license header to every new `.py` file:
  ```python
  # Copyright 2026 Cisco Systems, Inc. and its affiliates
  # SPDX-License-Identifier: Apache-2.0
  ```

## Contribution conventions

- **Backward compatibility**: Do not change a tool's existing output/behaviour unless clearly fixing a
  bug or improving it; document the change in the tool's README.
- **Code style**: PEP 8, type annotations on public functions, docstrings. Parse YAML with
  `yaml.safe_load()` (never `yaml.load()`). Keep each tool self-contained (own `README.md` +
  `requirements.txt`).
- See [CONTRIBUTING.md](./CONTRIBUTING.md) for the full contribution guide.
