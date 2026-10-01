# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""Blank Cyber Vision groups are drawn; named noise groups are not."""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]

_HEADER = (
    "Device Name;Device Custom Name;Device Type;Component Name;"
    "Component Custom Name;Group;IP;MAC"
)


def _load_cv():
    path = ROOT / "cv_converter" / "cv_to_ns_commands.py"
    spec = importlib.util.spec_from_file_location("cv_to_ns_commands", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cv = _load_cv()


class UngroupedAssetTests(unittest.TestCase):
    def test_shipped_config_does_not_treat_blank_group_as_noise(self):
        cfg = cv.load_config(cv.CONFIG_PATH)
        self.assertNotIn("", [g.strip() for g in cfg["noise_groups"]])
        self.assertIn("Broadcast Components", cfg["noise_groups"])

    def test_blank_group_is_drawn_and_named_noise_is_not(self):
        rows = [
            "PLC-1;PLC-1;Controller;PLC-1;;;192.168.50.10;00:11:22:33:44:55",
            "BCAST;BCAST;Controller;BCAST;;Broadcast Components;192.168.50.11;00:11:22:33:44:66",
            "CELL-1;CELL-1;Controller;CELL-1;;Process Bus Network;192.168.50.12;00:11:22:33:44:77",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            nodes = pathlib.Path(tmp) / "networkNodes.csv"
            nodes.write_text(_HEADER + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
            out = pathlib.Path(tmp) / "out"
            try:
                cv.main([
                    "--input-dir", tmp,
                    "--nodes", str(nodes),
                    "--output-dir", str(out),
                ])
            except SystemExit as exc:
                self.fail(f"converter exited {exc.code}")
            commands = (out / "gen_master_commands.txt").read_text(encoding="utf-8")
            self.assertIn("Ungrouped", commands)
            self.assertIn("PLC-1", commands)
            self.assertIn("Process Bus Network", commands)
            self.assertIn("CELL-1", commands)
            self.assertNotIn("BCAST", commands)
