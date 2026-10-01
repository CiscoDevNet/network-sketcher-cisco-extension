# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""Only an Attribute Model containing External Connector is a waypoint."""

from __future__ import annotations

import unittest

from cml_converter.src.ns_command_builder import build_command_script
from cml_converter.src.stencil_mapper import (
    StencilMapping, map_one, model_is_external_connector,
)
from cml_converter.src.topology_mapper import build_ns_model


def _node(label: str, node_definition: str, x: int) -> dict:
    return {
        "label": label,
        "node_definition": node_definition,
        "x": x,
        "y": 0,
        "interfaces": [],
    }


class ExternalConnectorWaypointTests(unittest.TestCase):
    def test_model_substring_is_the_only_waypoint_rule(self):
        self.assertTrue(model_is_external_connector(
            "External Connector (Bridge to host)"))
        self.assertFalse(model_is_external_connector("IOSv (Cisco IOS Router)"))
        self.assertFalse(model_is_external_connector("Unmanaged Switch"))

        nodes = [
            _node("inet", "external_connector", 0),
            _node("wan-rtr", "iosv", 100),
            _node("sw1", "unmanaged_switch", 200),
            _node("alias", "iosv", 50),
        ]
        stencils = {
            n["label"]: map_one(n["label"], n["node_definition"]) for n in nodes
        }
        stencils["alias"] = StencilMapping(
            label="alias", node_definition="iosv", image_definition="",
            stencil_type="Router", model="External Connector handoff",
            os="IOS", confidence=1.0, reason="test", tags=[],
        )
        model, _stats = build_ns_model(nodes, [], stencils, {})
        self.assertTrue(model.devices["inet"].area.endswith("_wp_"))
        self.assertTrue(model.devices["alias"].area.endswith("_wp_"))
        self.assertFalse(model.devices["wan-rtr"].area.endswith("_wp_"))
        self.assertFalse(model.devices["sw1"].area.endswith("_wp_"))

        script = build_command_script(model).text()
        self.assertIn("['inet', \\\"['WayPoint'", script)
        self.assertIn("['alias', \\\"['WayPoint'", script)
        self.assertIn("['wan-rtr', \\\"['DEVICE'", script)
        self.assertIn("['sw1', \\\"['DEVICE'", script)
        self.assertNotIn("wan-isn_wp_", script)
