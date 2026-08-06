# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""sanitize_vmanage_export.py -- turn a real vManage export into a safe,
committable sample for ``sdwan_converter``.

Replaces environment-specific identifiers (device ``uuid``, ``board-serial``,
``latitude``/``longitude``, every timestamp -- wherever nested -- and the
``_meta.host`` vManage URL) with obviously-synthetic values, while
leaving every field the converter's mapping logic actually reads --
``host-name``, ``site-name``, ``site-id``, ``personality``, ``device-type``,
``device-model``, all IP addresses/subnets, ``interfaces``, ``wan_interfaces``,
``bfd_sessions``, ``control_connections(_history)``, ``lldp_neighbors``,
``cdp_neighbors``, and ``vpn_names`` -- byte-for-byte unchanged.

Timestamps are matched by SHAPE rather than by an enumerated key list, so a
vManage schema that reports a capture time under a key this script has never
seen still gets normalized:

* A key whose name contains ``time``, ``date`` or ``stamp`` (case-insensitive)
  AND whose value is a bare 13-digit epoch-ms number (``lastupdated``,
  ``uptime-date``, ``last-conn-time-date``, ``downtime-date``,
  ``createTimeStamp``, ...) becomes :data:`FIXED_TIMESTAMP_MS`. Requiring both
  the name and the value shape keeps duration-style siblings such as
  ``uptime`` (``"0:01:18:50"``) and ``timezone`` (``"UTC +0000"``) untouched.
* Any string containing an ISO-8601 date-time substring has that substring --
  and only that substring -- replaced with :data:`FIXED_TIMESTAMP_ISO`, which
  denotes the same instant as :data:`FIXED_TIMESTAMP_MS`. This is shape-only
  (no key-name condition) because composite values such as
  ``vdevice-dataKey`` (``"100.0.0.102-0-0-2021-06-30T12:34:56+0000"``) embed a
  capture time under a key that names no time at all; the surrounding IP,
  index and separator structure is preserved.

``uuid`` and ``board-serial`` are remapped consistently: the same original
value always maps to the same synthetic value everywhere it appears (e.g. a
device's ``uuid`` and its later re-appearance as the fallback device key in
``control_connections_history``), so cross-references inside the export
still resolve after sanitization.

The script is idempotent: values already in synthetic form
(``SAMPLE-<HOST>-UUID``, ``SAMPLE0001``, the fixed timestamps) are passed
through untouched rather than being renumbered, so re-running it on the
committed sample cannot shuffle identities and break those cross-references.

Sanitization is fail-loud. After rewriting, the output is re-scanned for
anything that still looks environment-specific -- a foreign epoch-ms value, a
foreign ISO-8601 date-time, a canonical-format UUID, a ``uuid``/serial that is
not ``SAMPLE``-prefixed, real geo-coordinates or an unreplaced host URL. Any
hit is reported on stderr with its JSON path and the exit code is non-zero, so
a schema this script does not fully understand can never be mistaken for a
sanitized file. Pass ``--allow-residual`` to downgrade the failure to a
warning after reviewing every reported path.

Usage::

    python -m sdwan_converter.tools.sanitize_vmanage_export \\
        --input  sdwan_converter/Input_data/vmanage_export.json \\
        --output sdwan_converter/Input_data/sample_sdwan_export.json
"""
from __future__ import annotations

import argparse
import collections
import json
import pathlib
import re
import sys
from typing import Any, Dict, List, NamedTuple, Tuple

FIXED_TIMESTAMP_MS = 1700000000000  # 2023-11-14T22:13:20Z -- an arbitrary, obviously-synthetic epoch ms value
FIXED_TIMESTAMP_ISO = "2023-11-14T22:13:20+0000"  # the same instant as FIXED_TIMESTAMP_MS
FIXED_LATLON = "0.0"
SANITIZED_META_HOST = "https://203.0.113.10:8443"  # RFC 5737 TEST-NET-3 documentation address

_LATLON_KEYS = {"latitude", "longitude"}

_TIME_KEY_RE = re.compile(r"time|date|stamp", re.IGNORECASE)
_EPOCH_MS_RE = re.compile(r"^\d{13}$")
_EPOCH_ANY_RE = re.compile(r"^\d{10,}$")
_ISO_DATETIME_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
)
_CANONICAL_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_SANITIZED_UUID_RE = re.compile(r"^SAMPLE-.+-UUID$")
_SANITIZED_SERIAL_RE = re.compile(r"^SAMPLE\d{4}$")
_UUID_KEY_RE = re.compile(r"uuid", re.IGNORECASE)
_SERIAL_KEY_RE = re.compile(r"serial", re.IGNORECASE)

_RESIDUAL_EXAMPLES_PER_REASON = 10


class Residual(NamedTuple):
    """One value that still looks environment-specific after sanitization."""

    path: str
    key: str
    reason: str
    value: str


def _is_epoch_ms(value: Any) -> bool:
    """True if `value` is a bare 13-digit epoch-ms number (int or numeric str)."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return bool(_EPOCH_MS_RE.match(str(value)))
    if isinstance(value, str):
        return bool(_EPOCH_MS_RE.match(value))
    return False


def _normalize_epoch_ms(value: Any) -> Any:
    """Return the fixed epoch-ms value, keeping `value`'s int/str JSON type."""
    return str(FIXED_TIMESTAMP_MS) if isinstance(value, str) else FIXED_TIMESTAMP_MS


def _normalize_iso_datetimes(value: str) -> str:
    """Replace every ISO-8601 date-time substring with the fixed one."""
    return _ISO_DATETIME_RE.sub(FIXED_TIMESTAMP_ISO, value)


def _build_device_maps(devices: list) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Build old->new maps for `uuid` and `board-serial`, keyed by each
    device's own (real) values, so every later occurrence of that same real
    value -- anywhere else in the export -- maps to the same synthetic one.

    Values that are already synthetic get no map entry, so re-running the
    script leaves them exactly as they are instead of renumbering them.
    """
    uuid_map: Dict[str, str] = {}
    serial_map: Dict[str, str] = {}
    taken_serials = {
        dev.get("board-serial")
        for dev in devices
        if isinstance(dev.get("board-serial"), str)
        and _SANITIZED_SERIAL_RE.match(dev["board-serial"])
    }
    for idx, dev in enumerate(devices, start=1):
        host_name = str(dev.get("host-name") or f"device{idx}")
        old_uuid = dev.get("uuid")
        if (
            isinstance(old_uuid, str)
            and old_uuid
            and not _SANITIZED_UUID_RE.match(old_uuid)
            and old_uuid not in uuid_map
        ):
            uuid_map[old_uuid] = f"SAMPLE-{host_name.upper()}-UUID"
        old_serial = dev.get("board-serial")
        if (
            isinstance(old_serial, str)
            and old_serial
            and not _SANITIZED_SERIAL_RE.match(old_serial)
            and old_serial not in serial_map
        ):
            candidate = idx
            while f"SAMPLE{candidate:04d}" in taken_serials:
                candidate += 1
            new_serial = f"SAMPLE{candidate:04d}"
            serial_map[old_serial] = new_serial
            taken_serials.add(new_serial)
    return uuid_map, serial_map


def _sanitize(obj: Any, uuid_map: Dict[str, str], serial_map: Dict[str, str]) -> Any:
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            if key == "uuid" and isinstance(value, str):
                out[key] = uuid_map.get(value, value)
            elif key == "board-serial" and isinstance(value, str):
                out[key] = serial_map.get(value, value)
            elif key in _LATLON_KEYS:
                out[key] = FIXED_LATLON
            elif _TIME_KEY_RE.search(key) and _is_epoch_ms(value):
                out[key] = _normalize_epoch_ms(value)
            elif key == "host" and value != SANITIZED_META_HOST:
                out[key] = SANITIZED_META_HOST
            elif isinstance(value, str):
                out[key] = _normalize_iso_datetimes(value)
            else:
                out[key] = _sanitize(value, uuid_map, serial_map)
        return out
    if isinstance(obj, list):
        return [_sanitize(item, uuid_map, serial_map) for item in obj]
    if isinstance(obj, str):
        return _normalize_iso_datetimes(obj)
    return obj


def sanitize_export(data: Dict[str, Any]) -> Dict[str, Any]:
    """Return a sanitized deep copy of a combined vManage export."""
    devices = data.get("devices") or []
    uuid_map, serial_map = _build_device_maps(devices)
    return _sanitize(data, uuid_map, serial_map)


def _scan_value(key: str, value: Any, path: str, out: List[Residual]) -> None:
    if isinstance(value, bool) or value is None:
        return
    if _is_epoch_ms(value) and str(value) != str(FIXED_TIMESTAMP_MS):
        out.append(Residual(path, key, "epoch-ms timestamp", str(value)))
        return
    if (
        _TIME_KEY_RE.search(key)
        and isinstance(value, (int, float, str))
        and _EPOCH_ANY_RE.match(str(value))
        and str(value) != str(FIXED_TIMESTAMP_MS)
    ):
        out.append(Residual(path, key, "epoch timestamp (unexpected width)", str(value)))
        return
    if not isinstance(value, str):
        return
    for match in _ISO_DATETIME_RE.findall(value):
        if match != FIXED_TIMESTAMP_ISO:
            out.append(Residual(path, key, "ISO-8601 date-time", value))
            break
    if _CANONICAL_UUID_RE.match(value):
        out.append(Residual(path, key, "canonical-format UUID", value))
    elif _UUID_KEY_RE.search(key) and not _SANITIZED_UUID_RE.match(value):
        out.append(Residual(path, key, "non-SAMPLE uuid", value))
    if _SERIAL_KEY_RE.search(key) and not value.startswith("SAMPLE"):
        out.append(Residual(path, key, "non-SAMPLE serial", value))
    if key in _LATLON_KEYS and value != FIXED_LATLON:
        out.append(Residual(path, key, "real geo-coordinate", value))
    if key == "host" and value != SANITIZED_META_HOST:
        out.append(Residual(path, key, "unreplaced host URL", value))


def find_residuals(data: Any, path: str = "$") -> List[Residual]:
    """Scan sanitized data for values that still look environment-specific.

    Detection is deliberately broader than what :func:`sanitize_export`
    rewrites, so a vManage schema carrying identifying data under an
    unanticipated key is reported instead of silently passing through.
    """
    out: List[Residual] = []
    if isinstance(data, dict):
        for key, value in data.items():
            child = f"{path}.{key}"
            if isinstance(value, (dict, list)):
                out.extend(find_residuals(value, child))
            else:
                _scan_value(key, value, child, out)
    elif isinstance(data, list):
        for idx, item in enumerate(data):
            child = f"{path}[{idx}]"
            if isinstance(item, (dict, list)):
                out.extend(find_residuals(item, child))
            else:
                _scan_value("", item, child, out)
    return out


def report_residuals(residuals: List[Residual], stream: Any = sys.stderr) -> None:
    """Print a grouped, path-annotated residual report to `stream`."""
    by_reason: Dict[str, List[Residual]] = collections.defaultdict(list)
    for residual in residuals:
        by_reason[residual.reason].append(residual)
    print(
        f"ERROR: {len(residuals)} value(s) still look environment-specific "
        f"after sanitization:",
        file=stream,
    )
    for reason in sorted(by_reason):
        hits = by_reason[reason]
        keys = ", ".join(sorted({hit.key for hit in hits if hit.key})) or "(list items)"
        print(f"  [{reason}] {len(hits)} hit(s); keys: {keys}", file=stream)
        for hit in hits[:_RESIDUAL_EXAMPLES_PER_REASON]:
            print(f"    {hit.path} = {hit.value!r}", file=stream)
        if len(hits) > _RESIDUAL_EXAMPLES_PER_REASON:
            print(f"    ... and {len(hits) - _RESIDUAL_EXAMPLES_PER_REASON} more", file=stream)
    print(
        "Extend sanitize_vmanage_export.py to cover the keys above, or re-run "
        "with --allow-residual once you have confirmed every path is safe to "
        "publish.",
        file=stream,
    )


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", "-i", required=True, metavar="EXPORT_JSON")
    parser.add_argument("--output", "-o", required=True, metavar="SANITIZED_JSON")
    parser.add_argument(
        "--allow-residual",
        action="store_true",
        help="report residual environment-specific values but still exit 0",
    )
    args = parser.parse_args(argv)

    in_path = pathlib.Path(args.input)
    out_path = pathlib.Path(args.output)

    with in_path.open(encoding="utf-8") as fh:
        data = json.load(fh)

    sanitized = sanitize_export(data)

    residuals = find_residuals(sanitized)
    if residuals:
        report_residuals(residuals)
        if not args.allow_residual:
            print(
                f"Aborted: {out_path} NOT written (residual check failed).",
                file=sys.stderr,
            )
            return 1

    with out_path.open("w", encoding="utf-8") as fh:
        # indent=1 matches fetch_from_vmanage.py's own writer, so diffing a
        # fresh fetch against the committed sample shows only the values that
        # really changed, never a whole-file reindent.
        json.dump(sanitized, fh, indent=1, ensure_ascii=False)
        fh.write("\n")

    print(f"Sanitized export written to {out_path}")
    if residuals:
        print(
            f"WARNING: written with {len(residuals)} residual value(s) "
            f"(--allow-residual).",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
