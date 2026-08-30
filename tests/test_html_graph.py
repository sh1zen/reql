from __future__ import annotations

import json
import unittest

from memory.reporting.html_graph import render_graph_html


class HtmlGraphTests(unittest.TestCase):
    def test_html_graph_omits_test_nodes_and_marks_entrypoints(self) -> None:
        test_nodes = [
            {
                "id": f"function:test-{index}",
                "type": "Function",
                "label": f"test_case_{index}",
                "properties": {"relative_path": f"tests/test_case_{index}.py", "name": f"test_case_{index}"},
            }
            for index in range(120)
        ]
        payload = {
            "format": "reql-memory-export-v1",
            "nodes": [
                {
                    "id": "function:main",
                    "type": "Function",
                    "label": "main",
                    "properties": {"relative_path": "src/app.py", "name": "main"},
                },
                {
                    "id": "function:worker",
                    "type": "Function",
                    "label": "worker",
                    "properties": {"relative_path": "src/app.py", "name": "worker"},
                },
                *test_nodes,
            ],
            "edges": [
                {"id": "edge:main-worker", "from_id": "function:main", "to_id": "function:worker", "type": "CALLS"},
                {"id": "edge:test-worker", "from_id": "function:test-0", "to_id": "function:worker", "type": "CALLS"},
            ],
        }

        html = render_graph_html(payload)
        graph_payload = self._embedded_payload(html)

        self.assertEqual(graph_payload["source_counts"], {"nodes": 122, "edges": 2})
        self.assertEqual(graph_payload["visual_counts"], {"nodes": 2, "edges": 1, "tests_omitted": 120})
        self.assertEqual({node["id"] for node in graph_payload["nodes"]}, {"function:main", "function:worker"})
        self.assertTrue(next(node for node in graph_payload["nodes"] if node["id"] == "function:main")["entrypoint"])
        self.assertFalse(next(node for node in graph_payload["nodes"] if node["id"] == "function:worker")["entrypoint"])

    def test_html_graph_contains_one_depth_toggle_controls(self) -> None:
        html = render_graph_html({"nodes": [], "edges": []})

        self.assertIn("Entry points only", html)
        self.assertIn("function toggleNodeExpansion", html)
        self.assertIn("function rebuildRevealedNodes", html)
        self.assertIn("let expandedNodeIds = new Set()", html)
        self.assertIn('id="reset-graph"', html)
        self.assertIn('id="fit-graph"', html)

    @staticmethod
    def _embedded_payload(html: str) -> dict[str, object]:
        marker = '<script id="graph-data" type="application/json">'
        start = html.index(marker) + len(marker)
        end = html.index("</script>", start)
        return json.loads(html[start:end])


if __name__ == "__main__":
    unittest.main()
