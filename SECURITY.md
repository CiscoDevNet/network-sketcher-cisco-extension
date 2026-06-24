# Security Policies and Procedures

This document outlines security procedures and general policies for the
`network-sketcher-cisco-extension` project.

- [Reporting a Bug](#reporting-a-bug)
- [Disclosure Policy](#disclosure-policy)
- [Security Considerations](#security-considerations)
- [Comments on this Policy](#comments-on-this-policy)

## Reporting a Bug

The `network-sketcher-cisco-extension` team and community take all security bugs in
`network-sketcher-cisco-extension` seriously. Thank you for improving the security of
`network-sketcher-cisco-extension`. We appreciate your efforts and responsible disclosure and
will make every effort to acknowledge your contributions.

**Please do not report security vulnerabilities through public GitHub issues.**

Report security bugs by emailing `oss-security@cisco.com`.

The lead maintainer will acknowledge your email within 48 hours, and will send a
more detailed response within 48 hours indicating the next steps in handling
your report. After the initial reply to your report, the security team will
endeavor to keep you informed of the progress towards a fix and full
announcement, and may ask for additional information or guidance.

## Disclosure Policy

When the security team receives a security bug report, they will assign it to a
primary handler. This person will coordinate the fix and release process,
involving the following steps:

- Confirm the problem and determine the affected versions.
- Audit code to find any potential similar problems.
- Prepare fixes for all releases still under maintenance. These fixes will be
  released as quickly as possible.

## Security Considerations

The **conversion** step of every tool runs entirely on locally-sourced files (YAML, CSV, JSON, and
running-config text) and does not connect to any network. The conversion logic does **not**:

- Store or transmit credentials
- Execute arbitrary code from the input files

YAML is loaded with `yaml.safe_load()` to prevent arbitrary code execution via YAML deserialization
attacks.

Some tools ship an optional **read-only fetch helper** (e.g. `aci_converter`'s `fetch_from_apic`) that
connects to a Cisco controller's REST API to produce the local input file. These helpers:

- Read credentials only from environment variables (e.g. `ACI_PASSWORD`); credentials are never
  hardcoded, logged, or written to disk.
- Perform read-only API calls and do not modify the source platform.

## Comments on this Policy

If you have suggestions on how this process could be improved please submit a
pull request.
