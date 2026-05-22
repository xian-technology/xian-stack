from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from localnet_node_report import (  # noqa: E402
    collect_localnet_node_report,
    parse_prometheus_text,
)


METRICS_TEXT = """
# HELP xian_node Static Xian node runtime information.
# TYPE xian_node_info gauge
xian_node_info{chain_id="xian-local",execution_mode="xian_vm_v1",bds_enabled="false",parallel_execution_enabled="false",tx_fees_enabled="true"} 1
""".strip()


class LocalnetNodeReportTests(unittest.TestCase):
    def test_parse_prometheus_text_extracts_labels_and_values(self) -> None:
        samples = parse_prometheus_text(METRICS_TEXT)
        names = {sample["name"] for sample in samples}
        self.assertIn("xian_node_info", names)
        node_sample = next(
            sample for sample in samples if sample["name"] == "xian_node_info"
        )
        self.assertEqual(node_sample["labels"]["execution_mode"], "xian_vm_v1")
        self.assertEqual(node_sample["value"], 1.0)

    def test_collect_localnet_node_report_summarizes_nodes(self) -> None:
        network = {
            "execution": {"mode": "xian_vm_v1"},
            "nodes": [
                {
                    "moniker": "node-0",
                    "host_rpc_port": 36657,
                    "host_metrics_port": 37660,
                    "host_xian_metrics_port": 39108,
                },
                {
                    "moniker": "node-1",
                    "host_rpc_port": 36667,
                    "host_metrics_port": 37670,
                    "host_xian_metrics_port": 39208,
                },
            ],
        }

        def fake_fetch(url: str, *, timeout_seconds: float) -> str:
            if url.endswith("/status"):
                return json.dumps(
                    {
                        "result": {
                            "sync_info": {"latest_block_height": "17"},
                        }
                    }
                )
            if url.endswith(":39108/metrics") or url.endswith(":39208/metrics"):
                return METRICS_TEXT
            raise AssertionError(f"unexpected url {url}")

        with mock.patch("localnet_node_report.fetch_text", side_effect=fake_fetch):
            report = collect_localnet_node_report(
                network,
                timeout_seconds=5.0,
            )

        self.assertTrue(report["ok"])
        self.assertEqual(report["totals"]["node_count"], 2)
        self.assertEqual(
            report["expected_node_capabilities"]["execution_mode"],
            "xian_vm_v1",
        )
        self.assertTrue(report["checks"]["uniform_execution_mode"])
        self.assertTrue(report["checks"]["all_nodes_report_xian_vm"])


if __name__ == "__main__":
    unittest.main()
