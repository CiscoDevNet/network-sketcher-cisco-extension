# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0
"""Untagged (native) VLAN notation for Network Sketcher (RULE 9.5).

An L2 segment named ``Vlan1_UNTAG_`` is VLAN ``Vlan1`` carried untagged on
that port. Across an L1 link Network Sketcher connects two ``_UNTAG_``
segments whatever their names, connects two tagged segments only when the
names match, and never connects ``Vlan1_UNTAG_`` to ``Vlan1``. A port carries
at most one ``_UNTAG_`` segment.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

UNTAG_SUFFIX = "_UNTAG_"


def is_untag(name: str) -> bool:
    return name.endswith(UNTAG_SUFFIX)


def base_name(name: str) -> str:
    return name[: -len(UNTAG_SUFFIX)] if is_untag(name) else name


def untag(name: str) -> str:
    return name if is_untag(name) else name + UNTAG_SUFFIX


def vlan_segments(vids: List[int], native: "int | None") -> List[str]:
    """``Vlan<id>`` names, the native one marked ``_UNTAG_``."""
    return [f"Vlan{v}{UNTAG_SUFFIX}" if v == native else f"Vlan{v}" for v in vids]


def apply_untag_consistency(model) -> int:
    """Make both ends of every L1 link agree on the untagged VLAN.

    When one end carries ``X_UNTAG_`` and the other end carries the tagged
    ``X`` and no ``_UNTAG_`` of its own, the other end's ``X`` becomes
    ``X_UNTAG_`` -- an access port or a Dummy peer facing a trunk's native
    VLAN, for example. Without this the link that the tagged names used to
    connect would be cut. A port that already has an ``_UNTAG_`` (a native
    VLAN mismatch) is left alone. The segments of a Port-channel are read on
    the logical port, and a sub-interface binding counts for its parent
    port. Repeats until nothing changes, so a chain of Dummy devices follows.
    Returns the number of names rewritten.
    """
    pc_of: Dict[Tuple[str, str], str] = {}
    for pc in model.port_channels:
        for member in pc.physical_ports:
            pc_of[(pc.device, member)] = pc.portchannel_name

    def key(device: str, port: str) -> Tuple[str, str]:
        return device, pc_of.get((device, port), port)

    changed = 0
    for _ in range(16):
        segs: Dict[Tuple[str, str], List] = {}
        for seg in model.l2_segments_phys:
            segs.setdefault((seg.device, seg.port), []).append(seg)
        subs: Dict[Tuple[str, str], List] = {}
        for si in model.subinterfaces:
            if si.vlan_id is not None:
                subs.setdefault((si.device, si.parent_port), []).append(si)

        def names(k: Tuple[str, str]) -> List[str]:
            out = [n for seg in segs.get(k, []) for n in seg.vlans]
            out += [f"Vlan{si.vlan_id}{UNTAG_SUFFIX if getattr(si, 'native', False) else ''}"
                    for si in subs.get(k, [])]
            return out

        def convert(k: Tuple[str, str], tagged: str) -> bool:
            done = False
            for seg in segs.get(k, []):
                if tagged in seg.vlans:
                    seg.vlans = [untag(n) if n == tagged else n for n in seg.vlans]
                    done = True
            if not done:
                for si in subs.get(k, []):
                    if f"Vlan{si.vlan_id}" == tagged and not getattr(si, "native", False):
                        si.native = True
                        done = True
                        break
            return done

        round_changed = 0
        for lk in model.l1_links:
            a = key(lk.a_device, lk.a_port)
            b = key(lk.b_device, lk.b_port)
            for src, dst in ((a, b), (b, a)):
                src_untag = [n for n in names(src) if is_untag(n)]
                dst_names = names(dst)
                if len(src_untag) != 1 or any(is_untag(n) for n in dst_names):
                    continue
                tagged = base_name(src_untag[0])
                if tagged in dst_names and convert(dst, tagged):
                    round_changed += 1
        changed += round_changed
        if not round_changed:
            break
    return changed
