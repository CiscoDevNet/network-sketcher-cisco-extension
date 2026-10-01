# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the lightweight read-only fetch probes."""

from __future__ import annotations

import importlib.util
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from aci_converter.src import fetch_from_apic
from catc_converter.src import fetch_from_catc
from meraki_converter.src import fetch_from_meraki
from nd_converter.src import fetch_from_nd
from sdwan_converter.src import fetch_from_vmanage


ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_netbox_module():
    path = (ROOT / "3rd_party" / "netbox_converter" / "src"
            / "fetch_from_netbox.py")
    spec = importlib.util.spec_from_file_location("fetch_from_netbox", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fetch_from_netbox = _load_netbox_module()


class FetchProbeTests(unittest.TestCase):
    def _out(self, directory: str, name: str) -> str:
        return str(pathlib.Path(directory) / name)

    def test_apic_probe_reads_one_class_without_export(self) -> None:
        client = mock.Mock()
        client._request.return_value = {"imdata": []}
        with tempfile.TemporaryDirectory() as directory:
            out = self._out(directory, "apic.json")
            with mock.patch.object(fetch_from_apic, "ApicClient",
                                   return_value=client), \
                    mock.patch.dict(os.environ, {"ACI_PASSWORD": "test-only"}):
                result = fetch_from_apic.main(
                    ["--host", "apic.invalid", "--probe", "--out", out])
            self.assertEqual(result, 0)
            client.login.assert_called_once_with("admin", "test-only")
            client._request.assert_called_once_with(
                "/api/class/fabricNodeIdentP.json")
            self.assertFalse(pathlib.Path(out).exists())

    def test_catc_probe_reads_sites_without_export(self) -> None:
        client = mock.Mock()
        client.get.return_value = {"response": []}
        with tempfile.TemporaryDirectory() as directory:
            out = self._out(directory, "catc.json")
            with mock.patch.object(fetch_from_catc, "CatcClient",
                                   return_value=client), \
                    mock.patch.dict(os.environ, {"CATC_PASSWORD": "test-only"}):
                result = fetch_from_catc.main(
                    ["--host", "catc.invalid", "--probe", "--out", out])
            self.assertEqual(result, 0)
            client.get.assert_called_once_with("/dna/intent/api/v1/site")
            self.assertFalse(pathlib.Path(out).exists())

    def test_sdwan_probe_reads_device_inventory_without_export(self) -> None:
        client = mock.Mock()
        client.get.return_value = {"data": []}
        with tempfile.TemporaryDirectory() as directory:
            out = self._out(directory, "sdwan.json")
            with mock.patch.object(fetch_from_vmanage, "VmanageClient",
                                   return_value=client), \
                    mock.patch.dict(os.environ, {"VMANAGE_PASSWORD": "test-only"}):
                result = fetch_from_vmanage.main(
                    ["--host", "vmanage.invalid", "--probe", "--out", out])
            self.assertEqual(result, 0)
            client.get.assert_called_once_with("/dataservice/device")
            self.assertFalse(pathlib.Path(out).exists())

    def test_ndfc_probe_reads_fabrics_without_export(self) -> None:
        client = mock.Mock()
        client.get.return_value = []
        with tempfile.TemporaryDirectory() as directory:
            out = self._out(directory, "ndfc.json")
            with mock.patch.object(fetch_from_nd, "NdClient",
                                   return_value=client), \
                    mock.patch.dict(os.environ, {"ND_PASSWORD": "test-only"}):
                result = fetch_from_nd.main(
                    ["--host", "nd.invalid", "--probe", "--out", out])
            self.assertEqual(result, 0)
            client.get.assert_called_once_with(
                f"{fetch_from_nd._LAN}/control/fabrics")
            self.assertFalse(pathlib.Path(out).exists())

    def test_meraki_probe_reads_organization_without_export(self) -> None:
        client = mock.Mock()
        client.get.return_value = {"id": "123"}
        with tempfile.TemporaryDirectory() as directory:
            out = self._out(directory, "meraki.json")
            with mock.patch.object(fetch_from_meraki, "MerakiClient",
                                   return_value=client), \
                    mock.patch.dict(os.environ, {"MERAKI_API_KEY": "test-only"}):
                result = fetch_from_meraki.main(
                    ["--org-id", "123", "--probe", "--out", out])
            self.assertEqual(result, 0)
            client.get.assert_called_once_with("/organizations/123")
            self.assertFalse(pathlib.Path(out).exists())

    def test_netbox_probe_reads_status_and_one_sites_page(self) -> None:
        client = mock.Mock()
        client.base = "https://netbox.invalid"
        client.get_object.side_effect = [
            {"netbox-version": "4.0"},
            {"count": 0, "next": None, "results": []},
        ]
        with tempfile.TemporaryDirectory() as directory:
            out = self._out(directory, "netbox.json")
            with mock.patch.object(fetch_from_netbox, "NetboxClient",
                                   return_value=client), \
                    mock.patch.dict(os.environ, {"NETBOX_TOKEN": "test-only"}):
                result = fetch_from_netbox.main(
                    ["--url", "https://netbox.invalid", "--probe", "--out", out])
            self.assertEqual(result, 0)
            self.assertEqual(
                client.get_object.call_args_list,
                [mock.call("status/"), mock.call("dcim/sites/?limit=1")])
            self.assertFalse(pathlib.Path(out).exists())

    def test_probe_rejects_unexpected_json(self) -> None:
        client = mock.Mock()
        client.get.return_value = {"response": {}}
        with mock.patch.object(fetch_from_catc, "CatcClient",
                               return_value=client), \
                mock.patch.dict(os.environ, {"CATC_PASSWORD": "test-only"}):
            result = fetch_from_catc.main(
                ["--host", "catc.invalid", "--probe"])
        self.assertEqual(result, 1)


if __name__ == "__main__":
    unittest.main()
