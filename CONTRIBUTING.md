# How to Contribute

Thanks for your interest in contributing to `network-sketcher-cisco-extension`! Here are a few general guidelines on
contributing and reporting bugs that we ask you to review. Following these guidelines helps to communicate that you
respect the time of the contributors managing and developing this open source project. In return, they should
reciprocate that respect in addressing your issue, assessing changes, and helping you finalize your pull requests. In
that spirit of mutual respect, we endeavor to review incoming issues and pull requests within 10 days, and will close
any lingering issues or pull requests after 60 days of inactivity.

Please note that all of your interactions in the project are subject to our [Code of Conduct](/CODE_OF_CONDUCT.md). This
includes creation of issues or pull requests, commenting on issues or pull requests, and extends to all interactions in
any real-time space e.g., Slack, Discord, etc.

## Reporting Issues

Before reporting a new issue, please ensure that the issue was not already reported or fixed by searching through our
[issues list](https://github.com/CiscoDevNet/network-sketcher-cisco-extension/issues).

When creating a new issue, please be sure to include a **title and clear description**, as much relevant information as
possible, and, if possible, a test case. For this project it is especially helpful to include:

1. Steps to reproduce the issue
2. Expected vs. actual behaviour
3. Your OS, Python version, and dependency versions (`pip list`)
4. The tool and input format you used, and the relevant report
   (`parse_report.md` for `cml_converter`; `aci_underlay_report.md` / `aci_overlay_report.md` for `aci_converter`)

**If you discover a security bug, please do not report it through GitHub. Instead, please see security procedures in
[SECURITY.md](/SECURITY.md).**

## Sending Pull Requests

Before sending a new pull request, take a look at existing pull requests and issues to see if the proposed change or fix
has been discussed in the past, or if the change was already implemented but not yet released.

We follow semantic versioning and may reserve breaking changes until the next major version release. Please validate any
affected behaviour before submitting — this project currently relies on manual validation rather than an automated test
suite.

1. Fork the repository and create a feature branch:
   ```bash
   git checkout -b feature/my-feature
   ```
2. Make your changes. Follow the existing code style (see [Code Style](#code-style) below).
3. Validate your changes manually against your own export from the relevant Cisco platform (no live lab is bundled),
   for example:
   ```bash
   python -m cml_converter.src.convert --yaml /path/to/your_lab.yaml --out /tmp/test_out.txt
   ```
4. Commit with a clear message:
   ```bash
   git commit -m "feat: add support for XYZ export format"
   ```
5. Push your branch and open a Pull Request against `main`.

## Adding a New Tool

Each converter is self-contained. Place a new tool in its own sub-directory (e.g. `ise_converter/`) with:

- `README.md` — purpose, usage, example
- `requirements.txt` — minimal dependencies
- `src/` — Python source

Add a row to the tools table in the root [`README.md`](/README.md), and add the Cisco copyright / SPDX header (see
[Code Style](#code-style)) to every new `.py` file.

## Code Style

- Python 3.10+ (individual tools may support 3.8+; see each tool's README)
- Type annotations on all public functions, with docstrings
- `yaml.safe_load()` for YAML parsing (never `yaml.load()`)
- No hardcoded credentials or network calls in conversion logic; secrets come from environment variables
  (e.g. `ACI_PASSWORD`)
- Every `.py` file carries the license header:
  ```python
  # Copyright 2026 Cisco Systems, Inc. and its affiliates
  # SPDX-License-Identifier: Apache-2.0
  ```

## Other Ways to Contribute

We welcome anyone that wants to contribute to `network-sketcher-cisco-extension` to triage and reply to open issues to
help troubleshoot and fix existing bugs. Here is what you can do:

- Help ensure that existing issues follow the recommendations from the _[Reporting Issues](#reporting-issues)_ section,
  providing feedback to the issue's author on what might be missing.
- Review and update the existing content of our
  [Wiki](https://github.com/CiscoDevNet/network-sketcher-cisco-extension/wiki) with up-to-date instructions and code
  samples.
- Review existing pull requests, and test patches against real exports from the Cisco platforms that
  `network-sketcher-cisco-extension` targets.
- Write a test, or add a missing test case to an existing test.

## License

By contributing, you agree that your contributions will be licensed under the
[Apache License 2.0](./LICENSE).

Thanks again for your interest on contributing to `network-sketcher-cisco-extension`!

:heart:
