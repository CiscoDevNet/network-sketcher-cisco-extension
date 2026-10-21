# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""One CML lab fetched over the API becomes one Network Sketcher area."""

from __future__ import annotations

import os
import unittest

from cml_converter.src.convert import (
    _extract_configs_from_nodes, _rekey_configs, disambiguate_device_labels,
    merge_labs, sanitize_lab_area,
)
from cml_converter.src.fetch_from_cml import _LABS_PATH, choose_labs, main as fetch_main
from cml_converter.src.stencil_mapper import map_one
from cml_converter.src.topology_mapper import arrange_area_rows, build_ns_model


def _node(node_id: str, label: str, node_definition: str = "iosv",
          tags: list | None = None) -> dict:
    return {
        "id": node_id,
        "label": label,
        "node_definition": node_definition,
        "x": 0,
        "y": 0,
        "tags": tags or [],
        "interfaces": [{"id": "i0", "label": "GigabitEthernet0/0"}],
    }


def _areas(nodes: list, links: list | None = None) -> dict:
    stencils = {
        n["label"]: map_one(n["label"], n["node_definition"]) for n in nodes
    }
    model, _stats = build_ns_model(nodes, links or [], stencils, {})
    return {label: dev.area for label, dev in model.devices.items()}


class LabAreaTests(unittest.TestCase):
    def test_two_labs_stay_two_areas_even_with_site_tags(self):
        nodes, links = merge_labs([
            {
                "lab_title": "aarnet_au",
                "nodes": [_node("a1", "r1", tags=["site1"])],
                "links": [],
            },
            {
                "lab_title": "CSR 1000v Feature Tests",
                "nodes": [_node("b1", "s1-leaf", "iosvl2", tags=["site9"])],
                "links": [],
            },
        ])
        areas = _areas(nodes, links)
        self.assertEqual(areas["r1"], "aarnet_au")
        self.assertEqual(areas["s1-leaf"], "CSR 1000v Feature Tests")

    def test_yaml_node_without_stamp_still_uses_tag_heuristic(self):
        node = _node("n1", "s1-leaf", "iosvl2", tags=["site9"])
        areas = _areas([node])
        self.assertEqual(areas["s1-leaf"], "site9")

    def test_label_prefix_only_when_labs_share_a_name(self):
        nodes, _links = merge_labs([
            {
                "lab_title": "lab-a",
                "nodes": [_node("a1", "shared"), _node("a2", "only-a")],
                "links": [],
            },
            {
                "lab_title": "lab-b",
                "nodes": [_node("b1", "shared"), _node("b2", "only-b")],
                "links": [],
            },
        ])
        self.assertEqual(
            {n["label"] for n in nodes},
            {"lab-a_shared", "lab-b_shared", "only-a", "only-b"},
        )
        areas = _areas(nodes)
        self.assertEqual(areas["lab-a_shared"], "lab-a")
        self.assertEqual(areas["only-b"], "lab-b")

    def test_area_name_replaces_slash_backslash_and_quote(self):
        self.assertEqual(sanitize_lab_area("a/b\\c'd"), "a_b_c_d")
        nodes, _links = merge_labs([{
            "lab_title": "site/east",
            "nodes": [_node("n1", "r1")],
            "links": [],
        }])
        self.assertEqual(nodes[0]["_ns_area"], "site_east")
        self.assertEqual(_areas(nodes)["r1"], "site_east")

    def test_external_connector_stays_a_waypoint_inside_its_lab(self):
        nodes, _links = merge_labs([{
            "lab_title": "aarnet_au",
            "nodes": [
                _node("a1", "r1"),
                _node("a2", "inet", "external_connector"),
            ],
            "links": [],
        }])
        areas = _areas(nodes)
        self.assertEqual(areas["r1"], "aarnet_au")
        self.assertTrue(areas["inet"].startswith("aarnet_au-ext"))
        self.assertTrue(areas["inet"].endswith("_wp_"))


    def test_case_only_names_in_one_lab_stay_distinct(self):
        nodes, links = merge_labs([{
            "lab_title": "sdwan",
            "nodes": [
                {
                    **_node("n1", "External", "external_connector"),
                    "configuration": "hostname External",
                },
                {
                    **_node("n2", "external", "external_connector"),
                    "configuration": "hostname external",
                },
            ],
            "links": [{
                "id": "l1", "node_a": "n1", "node_b": "n2",
                "interface_a": "i0", "interface_b": "i0",
            }],
        }])
        labels = [n["label"] for n in nodes]
        self.assertEqual(len({label.casefold() for label in labels}), 2)
        self.assertEqual(labels, ["sdwan_External", "sdwan_external_2"])
        configs = _extract_configs_from_nodes(nodes)
        self.assertEqual(set(configs), set(labels))
        self.assertIn("hostname External", configs["sdwan_External"])
        self.assertIn("hostname external", configs["sdwan_external_2"])

        model, _stats = build_ns_model(
            nodes, links,
            {n["label"]: map_one(n["label"], n["node_definition"]) for n in nodes},
            {},
        )
        self.assertEqual(len(model.l1_links), 1)
        linked = {
            model.l1_links[0].a_device,
            model.l1_links[0].b_device,
        }
        self.assertEqual(linked, set(labels))

    def test_case_only_names_in_a_yaml_file_are_renamed(self):
        nodes = [_node("n1", "External"), _node("n2", "external")]
        disambiguate_device_labels(nodes)
        labels = [n["label"] for n in nodes]
        self.assertEqual(len({label.casefold() for label in labels}), 2)
        self.assertEqual(
            _rekey_configs({"External": "cfg-a", "external": "cfg-b"}, nodes),
            {labels[0]: "cfg-a", labels[1]: "cfg-b"},
        )

    def test_case_only_names_across_labs_are_prefixed(self):
        nodes, _links = merge_labs([
            {"lab_title": "lab-a", "nodes": [_node("a1", "R1")], "links": []},
            {"lab_title": "lab-b", "nodes": [_node("b1", "r1")], "links": []},
        ])
        labels = {n["label"] for n in nodes}
        self.assertEqual(labels, {"lab-a_R1", "lab-b_r1"})
        self.assertEqual(len({label.casefold() for label in labels}), 2)


class AreaRowTests(unittest.TestCase):
    def _no_adjacent_waypoints(self, rows):
        for row in rows:
            for left, right in zip(row, row[1:]):
                self.assertFalse(left.endswith("_wp_") and right.endswith("_wp_"),
                                 f"{left} and {right} are adjacent waypoint areas")

    def test_waypoint_areas_are_separated(self):
        rows = arrange_area_rows(["wan-isn_wp_", "default-ext_wp_", "default"])
        self._no_adjacent_waypoints(rows)
        self.assertEqual(rows, [["wan-isn_wp_", "default", "default-ext_wp_"]])

    def test_extra_waypoints_move_to_their_own_row(self):
        rows = arrange_area_rows(["a_wp_", "b_wp_", "c_wp_", "site1"])
        self._no_adjacent_waypoints(rows)
        self.assertEqual([area for row in rows for area in row],
                         ["a_wp_", "site1", "b_wp_", "c_wp_"])

    def test_order_is_kept_when_nothing_collides(self):
        ordered = ["CSR 1000v Feature Tests", "aarnet_au", "sdwan", "sdwan-ext_wp_"]
        self.assertEqual(arrange_area_rows(ordered), [ordered])

    def test_two_lab_areas_with_connectors_do_not_collide(self):
        nodes, _links = merge_labs([
            {
                "lab_title": "lab-a",
                "nodes": [_node("a1", "inet-a", "external_connector")],
                "links": [],
            },
            {
                "lab_title": "lab-b",
                "nodes": [_node("b1", "inet-b", "external_connector")],
                "links": [],
            },
        ])
        stencils = {
            n["label"]: map_one(n["label"], n["node_definition"]) for n in nodes
        }
        model, _stats = build_ns_model(nodes, [], stencils, {})
        self._no_adjacent_waypoints(model.areas)


class ChooseLabsTests(unittest.TestCase):
    def test_filter_by_title_or_id_and_keep_all_by_default(self):
        catalog = [("id-a", "aarnet_au"), ("id-b", "CSR 1000v Feature Tests")]
        self.assertEqual(choose_labs(catalog, None), catalog)
        self.assertEqual(choose_labs(catalog, ["id-b"]), [catalog[1]])
        self.assertEqual(choose_labs(catalog, ["aarnet_au"]), [catalog[0]])

    def test_unknown_lab_is_an_error(self):
        with self.assertRaises(RuntimeError):
            choose_labs([("id-a", "aarnet_au")], ["missing"])

    def test_lab_list_includes_other_users_labs(self):
        self.assertEqual(_LABS_PATH, "/api/v0/labs?show_all=true")

    def test_probe_without_a_password_does_not_connect(self):
        saved = os.environ.pop("CML_PASSWORD", None)
        try:
            rc = fetch_main(["--host", "cml.example.com", "--probe"])
        finally:
            if saved is not None:
                os.environ["CML_PASSWORD"] = saved
        self.assertEqual(rc, 2)
