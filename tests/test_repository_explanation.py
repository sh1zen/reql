from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from api import MemoryGraph
from memory import cli as cli_mod


class RepositoryExplanationTests(unittest.TestCase):
    def test_explanation_projects_code_into_business_capabilities_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = self._write_checkout_project(Path(td) / "checkout")
            graph = MemoryGraph.open(Path(td) / "memory.reql")
            try:
                result = graph.compile_project(root)
                self.assertEqual(result.run.status, "completed")
                counts_before = (graph.store.count_nodes(), graph.store.count_edges())
                project_status = graph.project_status(root)
                self.assertIsNotNone(project_status)

                with (
                    patch.object(
                        graph.projects,
                        "project_status",
                        return_value=project_status,
                    ),
                    patch.object(
                        graph.store,
                        "find_nodes_by_property",
                        wraps=graph.store.find_nodes_by_property,
                    ) as find_nodes,
                    patch.object(
                        graph.store,
                        "top_nodes_by_degree",
                        wraps=graph.store.top_nodes_by_degree,
                    ) as top_nodes,
                    patch.object(
                        graph.store,
                        "lexical_search",
                        wraps=graph.store.lexical_search,
                    ) as lexical_search,
                    patch.object(
                        graph.store,
                        "incident_edges",
                        wraps=graph.store.incident_edges,
                    ) as incident_edges,
                    patch.object(
                        graph.store,
                        "get_edges",
                        wraps=graph.store.get_edges,
                    ) as get_edges,
                ):
                    explanation = graph.explain_project(
                        root,
                        focus="checkout order",
                        max_capabilities=10,
                        max_workflows=5,
                    )

                self.assertEqual(counts_before, (graph.store.count_nodes(), graph.store.count_edges()))
                for mocked_query in (
                    find_nodes,
                    top_nodes,
                    lexical_search,
                    incident_edges,
                    get_edges,
                ):
                    self.assertTrue(mocked_query.call_args_list)
                    self.assertTrue(
                        all(call.kwargs.get("limit", call.kwargs.get("top_k")) is None for call in mocked_query.call_args_list)
                    )
                self.assertEqual(explanation.schema_version, 2)
                self.assertEqual(explanation.basis["mode"], "deterministic-code-graph")
                self.assertEqual(
                    explanation.basis["workflow_projection"],
                    "semantic-multi-evidence",
                )
                self.assertFalse(explanation.basis["persisted"])
                self.assertFalse(explanation.basis["llm_required"])
                self.assertGreaterEqual(len(explanation.capabilities), 4)
                self.assertIn("interface", {layer.name for layer in explanation.layers})
                self.assertIn("domain", {layer.name for layer in explanation.layers})
                self.assertIn("infrastructure", {layer.name for layer in explanation.layers})
                self.assertTrue(
                    any(
                        "checkout" in " ".join(capability.responsibilities).casefold()
                        for capability in explanation.capabilities
                    )
                )
                self.assertEqual(explanation.change_guide.focus, "checkout order")
                self.assertTrue(explanation.change_guide.start_here)
                self.assertTrue(
                    any(
                        item.path.endswith("checkout.py") or item.path.endswith("api.py")
                        for item in explanation.change_guide.start_here
                    )
                )
                self.assertTrue(
                    any("tests/test_checkout.py" in test for test in explanation.change_guide.verify_with)
                )
                self.assertTrue(explanation.workflows)
                checkout_workflow = next(
                    workflow
                    for workflow in explanation.workflows
                    if workflow.name == "Checkout Order"
                )
                self.assertTrue(checkout_workflow.intent)
                self.assertEqual(checkout_workflow.trigger, "Checkout Order")
                self.assertTrue(checkout_workflow.inputs)
                self.assertIn("bool", checkout_workflow.outputs)
                self.assertTrue(checkout_workflow.invariants)
                self.assertGreaterEqual(len(checkout_workflow.participants), 2)
                participant_labels = {
                    participant.target.label.rsplit(".", 1)[-1]
                    for participant in checkout_workflow.participants
                }
                self.assertTrue(
                    {"CheckoutService", "OrderRepository"}.issubset(participant_labels)
                )
                self.assertTrue(
                    all(
                        participant.relation == "implemented_by"
                        for participant in checkout_workflow.participants
                    )
                )
                self.assertTrue(
                    any(
                        item.reason == "docstring corroborates workflow intent"
                        for item in checkout_workflow.evidence
                    )
                )
                self.assertFalse(
                    any(
                        workflow.trigger == "Checkout Preview"
                        for workflow in explanation.workflows
                    )
                )
                for workflow in explanation.workflows:
                    self.assertEqual(workflow.steps, [])
                    for participant in workflow.participants:
                        self.assertNotEqual(participant.target.node_type, "Module")
                        self.assertFalse(participant.target.path.startswith("tests/"))
                        self.assertNotIn(
                            participant.target.label.rsplit(".", 1)[-1],
                            {"__init__", "_write_checkout_project"},
                        )

                payload = explanation.to_dict()
                self.assertEqual(payload["project"]["name"], "checkout")
                self.assertTrue(all(owner["location"] for item in payload["capabilities"] for owner in item["owners"]))
                self.assertTrue(all("steps" not in workflow for workflow in payload["workflows"]))
                markdown = explanation.to_markdown()
                self.assertIn("# Repository explanation: checkout", markdown)
                self.assertIn("## Business capabilities", markdown)
                self.assertIn("## Semantic workflows", markdown)
                self.assertIn("- Implemented by:", markdown)
                self.assertIn("## Change guidance", markdown)
            finally:
                graph.close()

    def test_project_explain_cli_supports_json_and_focus(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = self._write_checkout_project(Path(td) / "checkout")
            storage = Path(td) / "memory.reql"
            graph = MemoryGraph.open(storage)
            try:
                graph.compile_project(root)
            finally:
                graph.close()

            stdout = io.StringIO()
            stderr = io.StringIO()
            with patch.object(cli_mod.sys, "stdout", stdout), patch.object(cli_mod.sys, "stderr", stderr):
                result = cli_mod.main(
                    [
                        "--storage",
                        str(storage),
                        "project",
                        "explain",
                        str(root),
                        "--focus",
                        "checkout",
                        "--max-workflows",
                        "2",
                        "--json",
                    ]
                )

            self.assertEqual(result, 0, stderr.getvalue())
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["basis"]["focus"], "checkout")
            self.assertLessEqual(len(payload["workflows"]), 2)
            self.assertTrue(payload["capabilities"])

    @staticmethod
    def _write_checkout_project(root: Path) -> Path:
        source = root / "src" / "shop"
        (source / "application").mkdir(parents=True)
        (source / "domain").mkdir()
        (source / "infrastructure").mkdir()
        (root / "tests").mkdir(parents=True)
        for package in (
            source,
            source / "application",
            source / "domain",
            source / "infrastructure",
        ):
            (package / "__init__.py").write_text("", encoding="utf-8")
        (source / "api.py").write_text(
            "\n".join(
                [
                    "from shop.application.checkout import CheckoutService",
                    "",
                    "def checkout_order(order_id: str) -> bool:",
                    '    """Checkout an order while requiring a valid order id."""',
                    "    return run_checkout(order_id)",
                    "",
                    "def run_checkout(order_id: str) -> bool:",
                    "    return CheckoutService().checkout(order_id)",
                ]
            ),
            encoding="utf-8",
        )
        (source / "application" / "checkout.py").write_text(
            "\n".join(
                [
                    "from shop.domain.orders import Order",
                    "from shop.infrastructure.orders import OrderRepository",
                    "",
                    "class CheckoutService:",
                    "    def __init__(self) -> None:",
                    "        self.repository = OrderRepository()",
                    "",
                    "    def checkout(self, order_id: str) -> bool:",
                    "        order = Order(order_id)",
                    "        return self.repository.save(order)",
                ]
            ),
            encoding="utf-8",
        )
        (source / "domain" / "orders.py").write_text(
            "\n".join(
                [
                    "class Order:",
                    "    def __init__(self, order_id: str):",
                    "        self.order_id = order_id",
                    "",
                    "def checkout_preview(order_id: str) -> Order:",
                    "    return Order(order_id)",
                ]
            ),
            encoding="utf-8",
        )
        (source / "infrastructure" / "orders.py").write_text(
            "\n".join(
                [
                    "class OrderRepository:",
                    "    def save(self, order) -> bool:",
                    "        return bool(order.order_id)",
                ]
            ),
            encoding="utf-8",
        )
        (root / "tests" / "test_checkout.py").write_text(
            "\n".join(
                [
                    "from shop.api import checkout_order",
                    "",
                    "def test_checkout_order():",
                    "    assert checkout_order('order-1')",
                ]
            ),
            encoding="utf-8",
        )
        return root
